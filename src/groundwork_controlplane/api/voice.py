"""Voice channel REST API (T099, T100, T106).

Exposes the voice consent, enablement, chat, and approval endpoints, plus the real-time
Voice Live WebSocket (``/ws/voice``) — the product voice surface since the 2026-08-21 scope
decision: a custom web frontend over Azure AI Voice Live speech-to-speech, no ACS, no PSTN.
The former Speech-SDK STT WebSocket (``/ws/speech``) and server-side ``/tts`` route were
removed with that decision; the frontend streams duplex audio to Voice Live through this
module's relay instead.

Every plan/approval/deployment operation still flows through the identical real functions
every other channel uses (the deterministic-execution boundary: the orchestration seam has exactly
one real entry point, never a second, looser one a channel invents for itself):
``PlanningAgent.generate_plan``
→ ``seal_plan`` → ``plan_repository`` (plan creation), ``approval.service.record_approval``
with ``channel=ApprovalChannel.VOICE`` (approval), and
``api.deployments.queue_deployment_for_approval`` (admission + queuing).

The WebSocket authenticates before anything else: browsers cannot set headers on a WebSocket
handshake, so the first frame must be ``{"type": "auth", "token": "<bearer>"}`` and it is
validated by the same ``TokenValidator`` every HTTP route uses. A failed handshake closes the
socket before any model connection is opened. Approval deliberately stays on the proven HTTP
route (``/approve``, invoked by the frontend's Approve button) rather than becoming a tool the
realtime model can invoke mid-conversation — an approval must never be one more thing an LLM
can decide to do.

``voice_live_endpoint`` in ``Settings`` gates all voice endpoints: when unset, every route
returns 503 with a clear message — voice is built but not configured, not broken.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from collections import deque
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

from agent_framework import Message
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, ConfigDict, Field

from groundwork_channels.voice.consent import (
    CURRENT_DISCLOSURE_VERSION,
    OffshoreInferenceConsentStore,
)
from groundwork_channels.voice.enablement import VoiceEnablementGate
from groundwork_channels.voice.voicelive import (
    VoiceLiveBridge,
    VoiceLiveConfig,
    VoiceLiveError,
    VoiceLiveSessionLike,
    open_voice_live_session,
)
from groundwork_contracts.approval import ApprovalChannel, PendingApproval, ThresholdPolicy
from groundwork_contracts.errors import PlanValidationError
from groundwork_contracts.plan import DeploymentPlan
from groundwork_contracts.tenant import (
    CONVERSATION_RETENTION,
    ConversationChannel,
    ConversationRecord,
    ConversationTurn,
    OffshoreInferenceConsent,
)
from groundwork_controlplane.agents.hardening import PROMPT_HARDENING_PREAMBLE
from groundwork_controlplane.agents.planning import PlanningAgent
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthorizationError, CallerRole
from groundwork_controlplane.api.deployments import queue_deployment_for_approval
from groundwork_controlplane.api.lighthouse_onboarding import (
    build_tenant_onboarding_facts,
    onboarding_facts_response,
)
from groundwork_controlplane.api.plans import (
    get_authenticated_caller,
    get_blueprint,
    get_customer_tenant,
)
from groundwork_controlplane.api.tenants import (
    BootstrapIdentityNotConfiguredError,
    QuickOnboardParams,
    _build_onboarding_status,
    _default_subscriptionless_tenant,
    attach_offshore_inference_consent,
    bootstrap_subscription_identity,
    confirm_customer_consent_attestation,
    create_customer_tenant_record,
    grant_customer_ado_org_access,
    quick_onboard_tenant,
)
from groundwork_controlplane.approval.lookup import find_approval_by_plan_hash
from groundwork_controlplane.approval.plan_identity import seal_plan
from groundwork_controlplane.approval.service import record_approval
from groundwork_controlplane.costing.licensing import licensing_disclosure_for
from groundwork_shared.telemetry.scrubbing import scrub_text

_VOICE_BLUEPRINT_ID = "standard-production-fabric"

# Anything with ``.app.state`` — an HTTP request or an accepted WebSocket. The conversation and
# plan-sealing helpers are shared by both surfaces on purpose: the deterministic-execution
# boundary gives them one body to live in, whatever socket delivered the caller.
Conn = Request | WebSocket

router = APIRouter(prefix="/v1/voice", tags=["voice"])


def _threshold_policy(request: Request) -> ThresholdPolicy:
    """Same construction as ``approvals.py``'s own ``_threshold_policy`` — kept as a small local
    copy rather than importing a module-private helper across files (three similar
    lines beat a premature cross-module abstraction for something this trivial)."""
    governance = request.app.state.settings.governance
    return ThresholdPolicy(
        monthly_amount_aud=governance.approval_threshold_aud,
        approver_role=governance.approver_role,
        applies_to_environments=("production",),
    )


def _now(conn: Conn) -> datetime:
    now_fn = getattr(conn.app.state, "now_fn", None)
    return now_fn() if now_fn is not None else datetime.now(UTC)


class VoiceNotConfiguredError(HTTPException):
    """Voice endpoints exist but no Voice Live endpoint is configured."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail="Voice is built but not configured. Set GROUNDWORK_VOICE_LIVE_ENDPOINT.",
        )


async def _require_voice(request: Request) -> None:
    if not request.app.state.settings.voice_live_endpoint:
        raise VoiceNotConfiguredError()


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Real-time Voice Live WebSocket — the product voice surface (2026-08-21 scope decision)
# ---------------------------------------------------------------------------


def _generate_plan_tool_core() -> dict[str, Any]:
    """``name``/``description``/``parameters`` for the one tool the conversational model may
    call — the single source of truth for ``/chat`` and ``/ws/voice``, which each wrap it in
    their own API's required shape (see the two constants below). The model only ever supplies
    extracted parameters; the real plan is built by ``PlanningAgent.generate_plan`` and only
    returned once sealed and persisted (the deterministic-execution boundary).

    **Not one shared literal shape**, despite the two call sites looking alike: found live
    2026-08-24 that Chat Completions (``/chat``, via ``client.chat.completions.create(tools=)``)
    requires the nested ``{"type": "function", "function": {name, description, parameters}}``
    form, while Voice Live's ``session.update.tools`` requires a **flat**
    ``{"type": "function", name, description, parameters}`` form (verified against
    microsoft-foundry/voicelive-samples' own function-calling-quickstart.py:
    `FunctionTool(name=..., description=..., parameters=...)`, no nested `function` key). A
    single constant sent as-is to both broke one of them silently — Voice Live's own
    discriminated-union config validator rejected the whole session with a confusing
    `invalid_session_update_message` error that gave no hint the actual problem was in `tools`,
    not the top-level message.
    """
    return {
        "name": "generate_plan",
        "description": "Create a deployment plan once all required info is gathered",
        "parameters": {
            "type": "object",
            "properties": {
                "subscription_id": {"type": "string", "pattern": r"^[0-9a-fA-F-]{36}$"},
                "region": {
                    "type": "string",
                    "enum": ["australiaeast", "australiasoutheast"],
                },
                "fabric_capacity_sku": {"type": "string", "pattern": r"^F\d+$"},
                "notification_email": {
                    "type": "string",
                    "description": (
                        "Email address explicitly confirmed by the customer for "
                        "deployment-outcome notifications."
                    ),
                },
                "devops_organization_url": {
                    "type": "string",
                    "description": (
                        "The customer's own Azure DevOps organization URL "
                        "(https://dev.azure.com/<name>), if they named one. Optional."
                    ),
                },
                "fabric_capacity_admin_upn": {
                    "type": "string",
                    "description": (
                        "A user in the customer's tenant (user@domain) to administer the "
                        "billable Fabric capacity, if the customer named one. Optional."
                    ),
                },
            },
            "required": [
                "subscription_id",
                "region",
                "fabric_capacity_sku",
                "notification_email",
            ],
        },
    }


def _get_onboarding_status_tool_core() -> dict[str, Any]:
    return {
        "name": "get_onboarding_status",
        "description": (
            "Fetch read-only onboarding facts for this tenant: the exact Lighthouse command/link, "
            "current delegation status, and the manual Azure DevOps grant steps."
        ),
        "parameters": {"type": "object", "properties": {}, "required": []},
    }


def _create_tenant_tool_core() -> dict[str, Any]:
    return {
        "name": "create_tenant",
        "description": "Create this tenant's onboarding record before onboarding can proceed.",
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "minLength": 1},
            },
            "required": ["display_name"],
        },
    }


def _confirm_customer_consent_tool_core() -> dict[str, Any]:
    return {
        "name": "confirm_customer_consent",
        "description": "Operator attestation that the customer completed the admin-consent step.",
        "parameters": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "pattern": r"^[0-9a-fA-F-]{36}$"},
                "confirmation_note": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["tenant_id", "confirmation_note"],
        },
    }


def _grant_ado_org_access_tool_core() -> dict[str, Any]:
    return {
        "name": "grant_ado_org_access",
        "description": "Run the Azure DevOps entitlement grant for this tenant and verify it.",
        "parameters": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "pattern": r"^[0-9a-fA-F-]{36}$"},
            },
            "required": ["tenant_id"],
        },
    }


def _check_plan_status_tool_core() -> dict[str, Any]:
    return {
        "name": "check_plan_status",
        "description": (
            "Check the approval status of a plan this conversation already generated. Use the "
            "planId value from an earlier generate_plan result — never a value the customer "
            "reads out, since a plan hash is not something a caller can reliably speak."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "plan_id": {
                    "type": "string",
                    "pattern": r"^sha256:[0-9a-f]{64}$",
                    "description": "The planId returned by a prior generate_plan call.",
                },
            },
            "required": ["plan_id"],
        },
    }


def _trigger_bootstrap_identity_tool_core() -> dict[str, Any]:
    return {
        "name": "trigger_bootstrap_identity",
        "description": "Create the bootstrap managed identity once onboarding is truly ready.",
        "parameters": {
            "type": "object",
            "properties": {
                "tenant_id": {"type": "string", "pattern": r"^[0-9a-fA-F-]{36}$"},
                "subscription_id": {"type": "string", "pattern": r"^[0-9a-fA-F-]{36}$"},
            },
            "required": ["tenant_id", "subscription_id"],
        },
    }


def _quick_onboard_tool_core() -> dict[str, Any]:
    return {
        "name": "quick_onboard",
        "description": (
            "Single-call onboarding for the operator's own tenant: creates the tenant record, "
            "confirms consent, records offshore-inference consent, and optionally enables voice. "
            "Use only when the operator themselves is the tenant owner confirming every "
            "attestation in one step, not for onboarding a customer tenant."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "display_name": {"type": "string", "minLength": 1},
                "consent_note": {"type": "string", "minLength": 1, "maxLength": 500},
                "voice_enabled": {"type": "boolean"},
                "voice_note": {"type": "string", "minLength": 1, "maxLength": 500},
            },
            "required": ["display_name", "consent_note"],
        },
    }


_GENERATE_PLAN_TOOL_CORE = _generate_plan_tool_core()
_GET_ONBOARDING_STATUS_TOOL_CORE = _get_onboarding_status_tool_core()
_CREATE_TENANT_TOOL_CORE = _create_tenant_tool_core()
_CONFIRM_CUSTOMER_CONSENT_TOOL_CORE = _confirm_customer_consent_tool_core()
_GRANT_ADO_ORG_ACCESS_TOOL_CORE = _grant_ado_org_access_tool_core()
_CHECK_PLAN_STATUS_TOOL_CORE = _check_plan_status_tool_core()
_TRIGGER_BOOTSTRAP_IDENTITY_TOOL_CORE = _trigger_bootstrap_identity_tool_core()
_QUICK_ONBOARD_TOOL_CORE = _quick_onboard_tool_core()
# Voice Live session.update.tools: flat, per function-calling-quickstart.py.
_GENERATE_PLAN_TOOL_VOICE_LIVE = {"type": "function", **_GENERATE_PLAN_TOOL_CORE}
_GET_ONBOARDING_STATUS_TOOL_VOICE_LIVE = {
    "type": "function",
    **_GET_ONBOARDING_STATUS_TOOL_CORE,
}
_CREATE_TENANT_TOOL_VOICE_LIVE = {"type": "function", **_CREATE_TENANT_TOOL_CORE}
_CONFIRM_CUSTOMER_CONSENT_TOOL_VOICE_LIVE = {
    "type": "function",
    **_CONFIRM_CUSTOMER_CONSENT_TOOL_CORE,
}
_GRANT_ADO_ORG_ACCESS_TOOL_VOICE_LIVE = {
    "type": "function",
    **_GRANT_ADO_ORG_ACCESS_TOOL_CORE,
}
_CHECK_PLAN_STATUS_TOOL_VOICE_LIVE = {"type": "function", **_CHECK_PLAN_STATUS_TOOL_CORE}
_TRIGGER_BOOTSTRAP_IDENTITY_TOOL_VOICE_LIVE = {
    "type": "function",
    **_TRIGGER_BOOTSTRAP_IDENTITY_TOOL_CORE,
}
_QUICK_ONBOARD_TOOL_VOICE_LIVE = {"type": "function", **_QUICK_ONBOARD_TOOL_CORE}
# /chat used to need a second, nested "Chat Completions" shape for these same tools. Now that it
# calls the native client too (with auto function-invocation disabled — see
# groundwork_controlplane.agents.providers.foundry_openai), it shares the flat shape above with
# the WS relay: one set of tool schemas for both surfaces, not two that could drift apart.


def _normalise_subscription_id(raw: str) -> str:
    """Re-insert canonical GUID dashes (8-4-4-4-12) around whatever 32 hex characters ``raw``
    contains, ignoring wherever its own dashes/spaces landed.

    Found live 2026-08-24: a voice model reconstructing a long GUID one spoken character at a
    time reliably gets the *content* right (32 correct hex digits) well before it reliably gets
    *dash placement* right, and ``DeploymentPlan``'s schema correctly requires canonical
    placement (FR-011 - no loosening the schema itself). Dash position is pure presentation, not
    part of the subscription's real identity, so repairing it here is not a validation
    loosening: the 32 hex digits the caller actually said still must be exactly right, and a
    genuinely wrong subscription id (wrong digit, wrong count) still fails validation exactly as
    before, just with a clearer signal than "some malformed 36-character string".
    """
    hex_only = "".join(ch for ch in raw if ch in "0123456789abcdefABCDEF")
    if len(hex_only) != 32:
        return raw  # not repairable — let normal schema validation reject it with its own detail
    return f"{hex_only[0:8]}-{hex_only[8:12]}-{hex_only[12:16]}-{hex_only[16:20]}-{hex_only[20:32]}"


async def _run_generate_plan_tool(
    websocket: WebSocket,
    caller: AuthenticatedCaller,
    planning_agent: PlanningAgent,
    args: dict[str, Any],
) -> dict[str, Any]:
    """Execute the model's ``generate_plan`` extraction through the real pipeline.

    Identical tail to ``/chat``'s tool-call path: parameters → conversation summary →
    ``PlanningAgent.generate_plan`` → entitlement/region checks → ``seal_plan`` → persist →
    notification-email persistence. Returns the sealed plan dict for the function-call result.
    """
    blueprint = get_blueprint(_VOICE_BLUEPRINT_ID, websocket)
    notification_email: str | None = args.get("notification_email") or None
    subscription_id = _normalise_subscription_id(args["subscription_id"])
    # Fail fast, before spending a full planning-agent model call, on the one value the model
    # is most prone to submitting incomplete (found live 2026-08-24: nothing previously checked
    # this — an incomplete subscription_id only ever failed deep inside DeploymentPlan schema
    # validation, after a wasted model round-trip, with a generic error the model had no way to
    # act on specifically).
    hex_digit_count = sum(1 for ch in subscription_id if ch in "0123456789abcdefABCDEF")
    if hex_digit_count != 32:
        return {
            "status": "error",
            "message": (
                "subscription_id is incomplete — it must contain exactly 32 hexadecimal "
                "characters (a full UUID). Ask the customer for the rest of it; do not call "
                "generate_plan again until you have the complete value."
            ),
        }
    summary = _voice_conversation_summary(
        blueprint,
        subscription_id=subscription_id,
        region=args["region"],
        fabric_capacity_sku=args["fabric_capacity_sku"],
        notification_email=notification_email,
    )
    generated = await planning_agent.generate_plan(summary)
    sealed = await _seal_plan_for_caller(websocket, caller, generated)
    org_verdict = await _persist_engagement_details(
        websocket,
        tenant_id=caller.tenant_id,
        email=notification_email,
        devops_organization_url=args.get("devops_organization_url") or None,
        fabric_capacity_admin_upn=args.get("fabric_capacity_admin_upn") or None,
    )
    # The organization verdict travels in the function-call result so the realtime model can
    # relay it: a typo'd URL ("not_found") deserves one more conversational turn now, not a
    # halted deployment later.
    return {**sealed, "devops_organization_check": org_verdict}


async def _run_check_plan_status_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    """Report a plan's approval status only — never deployment progress, which is not wired to
    voice on purpose (see the system prompt's "AFTER THE PLAN IS READY" section)."""
    plan_id = str(args["plan_id"])
    plan_repository = conn.app.state.plan_repository
    sealed = await plan_repository.read(caller.tenant_id, plan_id)
    if sealed is None:
        return _tool_error("plan_not_found", "Double-check the plan ID, or generate a new plan.")

    approval = await find_approval_by_plan_hash(
        pending_repository=conn.app.state.pending_approval_repository,
        approval_repository=conn.app.state.approval_repository,
        tenant_id=caller.tenant_id,
        plan_hash=sealed.plan_hash,
    )
    if approval is None:
        status = "awaiting_approval"
        next_action = 'Tell the customer to say "approve" or tap Approve & Deploy when ready.'
    elif isinstance(approval, PendingApproval):
        status = "awaiting_second_approval"
        next_action = "A second, distinct approver still needs to approve this plan."
    else:
        status = "approved"
        next_action = (
            "Approved. Deployment progress itself is still not visible from here — tell the "
            "customer to check the app or their confirmation email."
        )
    return {"status": status, "planId": sealed.plan_hash, "next_action": next_action}


async def _run_get_onboarding_status_tool(
    conn: Conn, caller: AuthenticatedCaller
) -> dict[str, Any]:
    tenant_repository = conn.app.state.tenant_repository
    tenant = await tenant_repository.read(caller.tenant_id, caller.tenant_id)
    if tenant is None:
        raise AuthorizationError("no onboarding record exists for this tenant")
    try:
        facts = await build_tenant_onboarding_facts(
            tenant=tenant,
            settings=conn.app.state.settings,
            credential=conn.app.state.credential,
            http_client=getattr(conn.app.state, "lighthouse_http_client", None),
        )
    except HTTPException as exc:
        return _tool_error("get_onboarding_status_failed", scrub_text(str(exc.detail)))
    except Exception as exc:
        return _tool_error("get_onboarding_status_failed", scrub_text(str(exc)))
    return {
        **onboarding_facts_response(facts, tenant),
        "onboardingStatus": _build_onboarding_status(tenant).model_dump(by_alias=True),
    }


@dataclass(frozen=True, slots=True)
class _ToolDeniedResult:
    status: str
    reason: str
    next_action: str


def _tool_error(reason: str, next_action: str) -> dict[str, object]:
    return {
        "status": "error",
        "reason": reason,
        "next_action": next_action,
        "verified": False,
    }


def _tool_denied(reason: str) -> dict[str, object]:
    denied = _ToolDeniedResult(
        status="denied",
        reason=reason,
        next_action="Have a Groundwork operator perform this step.",
    )
    return {
        "status": denied.status,
        "reason": denied.reason,
        "next_action": denied.next_action,
        "verified": False,
    }


def _verification_payload(*, verified: bool, evidence: dict[str, object]) -> dict[str, object]:
    return {"verified": verified, "evidence": evidence}


async def _run_create_tenant_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    try:
        body = _default_subscriptionless_tenant(
            request=conn, caller=caller, display_name=str(args["display_name"])
        )
        created = await create_customer_tenant_record(body=body, request=conn, caller=caller)
    except AuthorizationError as exc:
        return _tool_denied(scrub_text(exc.detail))
    except HTTPException as exc:
        return _tool_error("create_tenant_failed", scrub_text(str(exc.detail)))

    reread = await conn.app.state.tenant_repository.read(created.tenant_id, created.tenant_id)
    verified = reread is not None and reread.consent_state.value == "pending"
    return {
        "status": "created",
        "tenantId": created.tenant_id,
        **_verification_payload(
            verified=verified,
            evidence={
                "tenantId": created.tenant_id,
                "consentState": reread.consent_state.value if reread is not None else None,
            },
        ),
        "next_action": (
            "Guide the customer through admin consent, Lighthouse delegation, and Azure DevOps "
            "onboarding."
            if verified
            else "Tenant record was written but could not be verified; re-check onboarding status."
        ),
    }


async def _run_confirm_customer_consent_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    tenant_id = str(args["tenant_id"])
    try:
        await confirm_customer_consent_attestation(
            tenant_id=tenant_id,
            note=str(args["confirmation_note"]),
            request=conn,
            caller=caller,
        )
    except AuthorizationError as exc:
        return _tool_denied(scrub_text(exc.detail))
    except HTTPException as exc:
        return _tool_error("confirm_customer_consent_failed", scrub_text(str(exc.detail)))

    reread = await conn.app.state.tenant_repository.read(tenant_id, tenant_id)
    verified = reread is not None and reread.consent_state.value == "granted"
    return {
        "status": "granted" if verified else "error",
        "tenantId": tenant_id,
        **_verification_payload(
            verified=verified,
            evidence={
                "consentState": reread.consent_state.value if reread is not None else None,
                "consentConfirmedBy": (
                    reread.consent_confirmed_by_object_id if reread is not None else None
                ),
                "consentConfirmationNote": (
                    reread.consent_confirmation_note if reread is not None else None
                ),
            },
        ),
        "next_action": (
            "Proceed to bootstrap once Lighthouse delegation and Azure DevOps gates are verified."
            if verified
            else (
                "Consent write could not be verified; re-check onboarding status before continuing."
            )
        ),
    }


async def _run_grant_ado_org_access_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    try:
        return await grant_customer_ado_org_access(
            tenant_id=str(args["tenant_id"]),
            request=conn,
            caller=caller,
        )
    except AuthorizationError as exc:
        return _tool_denied(scrub_text(exc.detail))
    except HTTPException as exc:
        return _tool_error("grant_ado_org_access_failed", scrub_text(str(exc.detail)))


async def _run_trigger_bootstrap_identity_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    tenant_id = str(args["tenant_id"])
    subscription_id = str(args["subscription_id"])
    tenant_repository = conn.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        return _tool_error("tenant_not_found", "Create the tenant onboarding record first.")

    try:
        facts = await build_tenant_onboarding_facts(
            tenant=tenant,
            settings=conn.app.state.settings,
            credential=conn.app.state.credential,
            http_client=getattr(conn.app.state, "lighthouse_http_client", None),
        )
    except HTTPException as exc:
        return _tool_error("trigger_bootstrap_identity_failed", scrub_text(str(exc.detail)))
    except Exception as exc:
        return _tool_error("trigger_bootstrap_identity_failed", scrub_text(str(exc)))
    entitlement = tenant.entitlement_for(subscription_id)
    remaining_gates: list[dict[str, str]] = []
    if tenant.consent_state.value != "granted":
        remaining_gates.append(
            {
                "gate": "consent",
                "next_action": (
                    "A Groundwork operator must attest the completed admin-consent step."
                ),
            }
        )
    if facts.delegation_state.value != "granted":
        remaining_gates.append(
            {
                "gate": "lighthouse_delegation",
                "next_action": (
                    "Guide the customer to run the Lighthouse command, then re-check status."
                ),
            }
        )
    if entitlement is None:
        remaining_gates.append(
            {
                "gate": "subscription_entitlement",
                "next_action": "Record this subscription on the tenant before bootstrap can run.",
            }
        )
    elif tenant.devops_organization_url is None:
        remaining_gates.append(
            {
                "gate": "devops_organization_url",
                "next_action": (
                    "Record the customer's Azure DevOps organization URL before bootstrap."
                ),
            }
        )
    if remaining_gates:
        return {
            "status": "not_ready",
            "verified": False,
            "tenantId": tenant_id,
            "subscriptionId": subscription_id,
            "remainingGates": remaining_gates,
            "next_action": "Complete the listed onboarding gates before retrying bootstrap.",
            "evidence": onboarding_facts_response(facts, tenant)["gates"],
        }

    try:
        await bootstrap_subscription_identity(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            request=conn,
            caller=caller,
        )
    except AuthorizationError as exc:
        return _tool_denied(scrub_text(exc.detail))
    except (BootstrapIdentityNotConfiguredError, HTTPException) as exc:
        detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
        return _tool_error("trigger_bootstrap_identity_failed", scrub_text(str(detail)))

    reread = await tenant_repository.read(tenant_id, tenant_id)
    verified_entitlement = reread.entitlement_for(subscription_id) if reread is not None else None
    verified = (
        verified_entitlement is not None
        and verified_entitlement.bootstrap_identity_resource_id is not None
    )
    return {
        "status": "created" if verified else "error",
        "tenantId": tenant_id,
        "subscriptionId": subscription_id,
        **_verification_payload(
            verified=verified,
            evidence={
                "bootstrapIdentityResourceId": (
                    verified_entitlement.bootstrap_identity_resource_id
                    if verified_entitlement is not None
                    else None
                ),
                "bootstrapIdentityClientId": (
                    verified_entitlement.bootstrap_identity_client_id
                    if verified_entitlement is not None
                    else None
                ),
            },
        ),
        "next_action": (
            "Onboarding is ready for normal planning conversation."
            if verified
            else "Bootstrap write completed but could not be verified; re-check onboarding status."
        ),
    }


async def _run_quick_onboard_tool(
    conn: Conn, caller: AuthenticatedCaller, args: dict[str, Any]
) -> dict[str, object]:
    """Run the single-call onboarding flow, restricted to the operator's own tenant.

    Quick-onboard collapses create + consent-confirm + offshore-consent + voice-enable into one
    operation, so the voice model supplies only what it can reliably extract (`display_name`,
    `consent_note`, and the optional voice toggle). The tenant identity and regions come from the
    authenticated caller, not from conversation content — the same caller-own-tenancy rule as
    ``_run_create_tenant_tool`` and the REST route's own ``caller.tid == tenantId`` guard.
    """
    voice_enabled = bool(args.get("voice_enabled", False))
    voice_note = args.get("voice_note")
    if voice_enabled and voice_note is None:
        return _tool_error(
            "voice_note_required",
            "voice_note is required when voice_enabled is true in quick_onboard.",
        )

    params = QuickOnboardParams(
        tenant_id=caller.tenant_id,
        display_name=str(args["display_name"]),
        approved_regions=frozenset({conn.app.state.settings.azure_location}),
        data_residency_regions=frozenset({conn.app.state.settings.azure_location}),
        consent_note=str(args["consent_note"]),
        voice_enabled=voice_enabled,
        voice_note=str(voice_note) if voice_note is not None else None,
    )
    try:
        status = await quick_onboard_tenant(params=params, request=conn, caller=caller)
    except AuthorizationError as exc:
        return _tool_denied(scrub_text(exc.detail))
    except HTTPException as exc:
        return _tool_error("quick_onboard_failed", scrub_text(str(exc.detail)))

    step_completion = {name: step.completed for name, step in status.steps.items()}
    all_complete = all(step.completed for step in status.steps.values())
    return {
        "status": "onboarded" if all_complete else "partial",
        "tenantId": status.tenant_id,
        "consentState": status.consent_state,
        "steps": step_completion,
        **_verification_payload(
            verified=all_complete,
            evidence={
                "consentState": status.consent_state,
                "voiceEnabled": step_completion.get("voiceEnabled", False),
                "offshoreConsentRecorded": step_completion.get("offshoreConsentRecorded", False),
            },
        ),
        "next_action": status.next_action,
    }


@router.websocket("/ws/voice/{session_id}")
async def voice_live_websocket(websocket: WebSocket, session_id: str) -> None:
    """Real-time duplex voice session: browser audio ↔ this relay ↔ Azure AI Voice Live.

    Protocol, in order:

    1. **Auth first frame** — ``{"type": "auth", "token": "<bearer>"}``, validated by the same
       ``TokenValidator`` every HTTP route uses. Browsers cannot set headers on a WebSocket
       handshake, so the token arrives as a frame; nothing else is read before it validates,
       and no model connection is opened for an unauthenticated socket.
    2. **Enablement gate** (FR-053e) — per-tenant voice availability, including the recorded
       offshore-inference consent state (FR-053d): Voice Live inference may leave the geography,
       and a tenant without recorded consent gets a closed socket, not a degraded session.
    3. **Relay** — binary frames are customer microphone PCM16, forwarded to Voice Live;
       ``response.audio_delta`` payloads are forwarded back as binary. Transcript events are
       persisted to the tenant's ``ConversationRecord`` (FR-053b) exactly as ``/chat`` persists
       turns. Raw audio is never buffered or stored (FR-053a).
    4. **Shared onboarding + planning tools** — when the realtime model calls any onboarding or
       planning tool, the arguments run through the identical real pipeline as every other
       channel. ``generate_plan`` sends the sealed plan to the frontend as a ``{"type": "plan"}``
       event; the other tools return structured onboarding results. Approval stays on the HTTP
       route.
    """
    app = websocket.app
    settings = app.state.settings
    if not settings.voice_live_endpoint:
        await websocket.close(code=1011, reason="Voice not configured")
        return

    await websocket.accept()

    # --- auth handshake -----------------------------------------------------
    try:
        raw = await asyncio.wait_for(websocket.receive_text(), timeout=10)
        hello = json.loads(raw)
        token = hello.get("token") if isinstance(hello, dict) else None
        if not isinstance(token, str) or not token:
            raise ValueError("auth frame missing token")
        caller: AuthenticatedCaller = app.state.token_validator.validate(f"Bearer {token}")
    except WebSocketDisconnect:
        return
    except Exception as exc:
        # Never echo why beyond "failed" to the CALLER — invalid vs expired is attacker-relevant
        # detail. The server log is not the wire response, though: log the real reason there so
        # this is diagnosable without guessing, same as any other internal error log.
        logger.info("voice ws auth failed session=%s: %s: %s", session_id, type(exc).__name__, exc)
        await websocket.close(code=4401, reason="authentication failed")
        return

    # --- enablement gate (FR-053e/FR-053d) ----------------------------------
    gate: VoiceEnablementGate = app.state.voice_gate
    enablement = await gate.check(caller.tenant_id)
    if not enablement.may_accept:
        await websocket.send_json(
            {"type": "error", "message": f"voice not enabled for this tenant: {enablement.reason}"}
        )
        await websocket.close(code=4403, reason="voice not enabled")
        return

    planning_agent: PlanningAgent | None = getattr(app.state, "planning_agent", None)
    if planning_agent is None:
        await websocket.send_json({"type": "error", "message": "planning agent not wired"})
        await websocket.close(code=1011, reason="planning agent not wired")
        return

    # --- open the Voice Live session ----------------------------------------
    config = VoiceLiveConfig(endpoint_url=settings.voice_live_endpoint)
    try:
        vl_session: VoiceLiveSessionLike = await open_voice_live_session(
            endpoint_base=settings.voice_live_endpoint,
            credential=app.state.credential,
            config=config,
        )
    except VoiceLiveError as exc:
        await websocket.send_json({"type": "error", "message": str(exc)})
        await websocket.close(code=1011, reason="voice live unavailable")
        return
    except Exception:
        # Anything open_voice_live_session raises that isn't its own VoiceLiveError was falling
        # through uncaught here, closing the socket with a bare TCP reset (no WS close frame, no
        # client-visible reason, no server-side log line) instead of the graceful path above.
        logger.exception("voice ws open_voice_live_session failed session=%s", session_id)
        await websocket.close(code=1011, reason="voice live connection failed")
        return

    bridge = VoiceLiveBridge(vl_session, config=config)

    # Configure the realtime session: persona, the one tool, en-AU voice, semantic VAD so the
    # customer can talk over the agent naturally. Event shapes and the function-call flow below
    # follow the canonical sample ([VERIFIED] 2026-08-21):
    # github.com/microsoft-foundry/voicelive-samples python/voice-live-quickstarts/
    # function-calling-quickstart.py — audio defaults to PCM16 24 kHz; transcription pinned to
    # azure-speech/en-AU (FR-004a); tools live at session level.
    await vl_session.send_event(
        {
            "type": "session.update",
            "session": {
                "instructions": _CONVERSATION_SYSTEM,
                "modalities": ["text", "audio"],
                "voice": {"name": config.voice, "type": "azure-standard"},
                "input_audio_format": "pcm16",
                "output_audio_format": "pcm16",
                "input_audio_noise_reduction": {"type": "azure_deep_noise_suppression"},
                "input_audio_echo_cancellation": {"type": "server_echo_cancellation"},
                "turn_detection": {"type": "azure_semantic_vad"},
                "input_audio_transcription": {"model": "azure-speech", "language": "en-AU"},
                "tools": [
                    _GENERATE_PLAN_TOOL_VOICE_LIVE,
                    _GET_ONBOARDING_STATUS_TOOL_VOICE_LIVE,
                    _CREATE_TENANT_TOOL_VOICE_LIVE,
                    _CONFIRM_CUSTOMER_CONSENT_TOOL_VOICE_LIVE,
                    _GRANT_ADO_ORG_ACCESS_TOOL_VOICE_LIVE,
                    _CHECK_PLAN_STATUS_TOOL_VOICE_LIVE,
                    _TRIGGER_BOOTSTRAP_IDENTITY_TOOL_VOICE_LIVE,
                    _QUICK_ONBOARD_TOOL_VOICE_LIVE,
                ],
                "tool_choice": "auto",
                "temperature": config.temperature,
            },
        }
    )

    conversation, existed, etag = await _load_conversation(
        websocket, caller=caller, session_id=session_id
    )

    async def persist_turn(speaker: str, text: str) -> None:
        nonlocal conversation
        conversation = _append_turn(
            conversation, speaker=speaker, text=text, occurred_at=_now(websocket)
        )
        # Sequential write within one socket session — same no-concurrent-writer rationale
        # as /chat's agent-reply save.
        await _save_conversation(websocket, record=conversation, existed=True)

    # Persist the record once up-front so a brand-new session exists in Cosmos even if the
    # customer hangs up before finishing a sentence.
    conversation = await _save_conversation(
        websocket, record=conversation, existed=existed, etag=etag
    )

    # Function-call state machine, exactly as the canonical sample runs it: the call item
    # arrives first (conversation.item.created, carrying name/call_id/previous_item_id but no
    # arguments), the arguments arrive separately (response.function_call_arguments.done), and
    # only when the response completes (response.done) is the call executed, so the model is
    # never mid-response while its tool runs.
    pending_call: dict[str, str] | None = {}
    active_response = False
    greeted = False
    # A typed utterance that arrived while a response was already active (most commonly: the
    # customer types before the proactive greeting finishes). Cancelling the in-flight response
    # and immediately requesting a new one races the Realtime API's single-active-response
    # constraint — found live 2026-09-07, reproduced by typing right after connecting: the
    # server returns `conversation_already_has_active_response` and the typed message is silently
    # dropped. Queuing it here and requesting the response only once `response.done` confirms the
    # previous one actually finished avoids the race.
    pending_text: str | None = None

    async def execute_pending_call() -> None:
        nonlocal pending_call
        if pending_call is None:  # unreachable by construction; guards the type, not the flow
            return
        call_id = pending_call["call_id"]
        tool_name = pending_call.get("name", "")
        try:
            args = json.loads(pending_call.get("arguments", "{}"))
            if tool_name == "generate_plan":
                sealed = await _run_generate_plan_tool(websocket, caller, planning_agent, args)
            elif tool_name == "get_onboarding_status":
                sealed = await _run_get_onboarding_status_tool(websocket, caller)
            elif tool_name == "create_tenant":
                sealed = await _run_create_tenant_tool(websocket, caller, args)
            elif tool_name == "confirm_customer_consent":
                sealed = await _run_confirm_customer_consent_tool(websocket, caller, args)
            elif tool_name == "grant_ado_org_access":
                sealed = await _run_grant_ado_org_access_tool(websocket, caller, args)
            elif tool_name == "check_plan_status":
                sealed = await _run_check_plan_status_tool(websocket, caller, args)
            elif tool_name == "quick_onboard":
                sealed = await _run_quick_onboard_tool(websocket, caller, args)
            else:
                sealed = await _run_trigger_bootstrap_identity_tool(websocket, caller, args)
        except AuthorizationError as exc:
            # Onboarding-incomplete (FR-006) is not a technical failure and "please retry" is
            # actively wrong advice for it — nothing changes on retry until an operator completes
            # onboarding. Give the model a specific, narratable reason instead of the generic
            # message below, mirroring how the subscription_id completeness check already gives
            # the model an actionable reason rather than a bare failure. Distinguished from the
            # entitlement-mismatch AuthorizationError (wrong subscription_id — the customer should
            # re-check the value, not wait for an operator) by message content, since both share
            # the same exception type.
            logger.info("voice ws %s refused session=%s: %s", tool_name, session_id, exc)
            if "consent state" in str(exc):
                await bridge.send_function_result(
                    call_id,
                    {
                        "status": "onboarding_incomplete",
                        "message": (
                            "This tenant has not completed onboarding yet. Tell the customer "
                            "that before a plan can be generated, their organisation's "
                            "administrator needs to complete a one-time authorisation step, and "
                            "that Groundwork will follow up separately with the details. Do not "
                            "attempt generate_plan again this call."
                        ),
                    },
                    previous_item_id=pending_call.get("previous_item_id"),
                )
                return
            await bridge.send_function_result(
                call_id,
                {
                    "status": "error",
                    "message": (
                        "This subscription is not authorised for this tenant. Ask the customer "
                        "to double-check the subscription ID and try again."
                    ),
                },
                previous_item_id=pending_call.get("previous_item_id"),
            )
            return
        except PlanValidationError as exc:
            # FR-011: the model's plan failed schema validation (missing/extra/malformed field).
            # This is not a transient failure — "please retry" (the generic Exception branch
            # below) is actively wrong advice here, the same reasoning the subscription_id
            # length check above already applies. exc.errors carries the specific field-by-field
            # failures; give them to the model so it can ask the customer for what's actually
            # wrong instead of blindly repeating the same request.
            logger.warning(
                "voice ws generate_plan failed schema validation session=%s: %s",
                session_id,
                exc.errors,
            )
            field_summary = "; ".join(exc.errors) if exc.errors else exc.detail
            await bridge.send_function_result(
                call_id,
                {
                    "status": "error",
                    "message": (
                        "The plan could not be built — it failed validation: "
                        f"{field_summary}. This will not succeed on a bare retry. Work out what "
                        "specifically needs to change, tell the customer, and only call "
                        "generate_plan again once you have corrected information."
                    ),
                },
                previous_item_id=pending_call.get("previous_item_id"),
            )
            return
        except Exception as exc:  # the model must get a result either way
            # PlanValidationError.errors carries the specific field-by-field validation
            # failures (which field, what went wrong) but its own __str__/base Exception
            # message never includes them — logging just `exc` here silently discarded
            # exactly the detail needed to diagnose a real failure, every time, found live
            # 2026-08-24 debugging the first real end-to-end voice plan-generation attempt.
            field_errors = getattr(exc, "errors", None)
            logger.warning(
                "voice ws generate_plan failed session=%s: %s: %s%s",
                session_id,
                type(exc).__name__,
                exc,
                f" | field errors: {field_errors}" if field_errors else "",
            )
            await bridge.send_function_result(
                call_id,
                {"status": "error", "message": "tool execution failed; please retry"},
                previous_item_id=pending_call.get("previous_item_id"),
            )
            return
        if tool_name == "generate_plan":
            await bridge.send_function_result(
                call_id,
                {"status": "plan_ready"},
                previous_item_id=pending_call.get("previous_item_id"),
            )
            await websocket.send_json({"type": "plan", "plan": sealed})
            return
        await bridge.send_function_result(
            call_id,
            {"status": "ok", "result": sealed},
            previous_item_id=pending_call.get("previous_item_id"),
        )

    async def client_to_voice_live() -> None:
        nonlocal pending_text
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                return
            data = message.get("bytes")
            if data is not None:
                await bridge.send_audio(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            frame = json.loads(text)
            if frame.get("type") == "text" and str(frame.get("text", "")).strip():
                utterance = str(frame["text"]).strip()
                await persist_turn("customer", utterance)
                if active_response:
                    # Same tolerance as the barge-in path below: cancelling a response that has
                    # already finished server-side returns a benign "no active response" error.
                    pending_text = utterance
                    with suppress(Exception):
                        await vl_session.send_event({"type": "response.cancel"})
                    await vl_session.send_event(
                        {
                            "type": "conversation.item.create",
                            "item": {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": utterance}],
                            },
                        }
                    )
                else:
                    await vl_session.send_text_input(utterance)

    async def voice_live_to_client() -> None:
        nonlocal pending_call, active_response, greeted, pending_text
        async for event in bridge.receive_events():
            etype = event.get("type")
            if etype == "session.updated":
                if not greeted:
                    greeted = True
                    # Mark the response active, and actually request it, before telling the
                    # client we're ready — found live 2026-09-07: the client only acts after
                    # receiving "ready", but the old order sent "ready" first, leaving a window
                    # where a client fast enough to reply instantly could still send text before
                    # this response.create even went out, let alone before the server's own
                    # response.created echo came back to flip the flag. Setting the flag and
                    # dispatching the request first closes that window: the client physically
                    # cannot observe "ready" before active_response is already true.
                    active_response = True
                    # Proactive greeting, as the canonical sample does once the session is up.
                    await vl_session.send_event({"type": "response.create"})
                    await websocket.send_json({"type": "ready"})
            elif etype == "response.audio.delta":
                delta = event.get("delta")
                if isinstance(delta, str):
                    await websocket.send_bytes(base64.b64decode(delta))
            elif etype == "response.created":
                active_response = True
            elif etype == "input_audio_buffer.speech_started":
                # Barge-in: flush whatever is queued for playback so the customer is not still
                # hearing the interrupted sentence, and cancel the in-flight response as the
                # sample does (a benign "no active response" error is expected and ignored).
                await websocket.send_json({"type": "speech_started"})
                if active_response:
                    with suppress(Exception):
                        await vl_session.send_event({"type": "response.cancel"})
            elif etype == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript")
                if isinstance(transcript, str) and transcript.strip():
                    await persist_turn("customer", transcript.strip())
                    await websocket.send_json(
                        {"type": "transcript", "role": "customer", "text": transcript.strip()}
                    )
            elif etype == "response.audio_transcript.done":
                transcript = event.get("transcript")
                if isinstance(transcript, str) and transcript.strip():
                    await persist_turn("agent", transcript.strip())
                    await websocket.send_json(
                        {"type": "transcript", "role": "agent", "text": transcript.strip()}
                    )
            elif etype == "conversation.item.created":
                item = event.get("item")
                if isinstance(item, dict) and item.get("type") == "function_call":
                    name = item.get("name")
                    call_id = item.get("call_id")
                    previous_item_id = item.get("id")
                    if name in {
                        "generate_plan",
                        "get_onboarding_status",
                        "create_tenant",
                        "confirm_customer_consent",
                        "grant_ado_org_access",
                        "check_plan_status",
                        "trigger_bootstrap_identity",
                    } and isinstance(call_id, str):
                        pending_call = {
                            "name": name,
                            "call_id": call_id,
                            "previous_item_id": (
                                previous_item_id if isinstance(previous_item_id, str) else ""
                            ),
                        }
            elif etype == "response.function_call_arguments.done":
                if pending_call is not None and event.get("call_id") == pending_call["call_id"]:
                    arguments = event.get("arguments")
                    pending_call["arguments"] = arguments if isinstance(arguments, str) else "{}"
            elif etype == "response.done":
                active_response = False
                if pending_call is not None and "arguments" in pending_call:
                    await execute_pending_call()
                    pending_call = None
                elif pending_text is not None:
                    # The conversation.item.create for this text was already sent when it
                    # arrived (see client_to_voice_live) — only the response was deferred, to
                    # avoid requesting one while the cancelled response was still finishing.
                    pending_text = None
                    # Set before the await, same reasoning as the greeting above: a message
                    # arriving in client_to_voice_live while this coroutine is suspended on the
                    # send must see a response already active, not the momentary False set at
                    # the top of this handler.
                    active_response = True
                    await vl_session.send_event({"type": "response.create"})
            elif etype == "error":
                message = str(event.get("error"))
                if "no active response" in message.lower():
                    continue  # benign: barge-in cancel raced an already-finished response
                await websocket.send_json({"type": "error", "message": message})

    client_task = asyncio.create_task(client_to_voice_live())
    vl_task = asyncio.create_task(voice_live_to_client())
    try:
        done, _pending = await asyncio.wait(
            {client_task, vl_task}, return_when=asyncio.FIRST_EXCEPTION
        )
        for task in done:
            task.result()  # propagate real bugs; disconnects surface as normal returns
    except Exception:
        # A raw ASGI-level exception here (as opposed to an HTTP route) does not reliably
        # surface a traceback through the default exception middleware, and an uncaught
        # exception between accept() and here closes the socket with a bare TCP reset instead
        # of a WebSocket close frame — the client sees a dead connection with no reason, and the
        # operator sees nothing in logs unless it's logged explicitly, right here.
        logger.exception("voice ws session failed session=%s", session_id)
        raise
    finally:
        client_task.cancel()
        vl_task.cancel()
        await bridge.close()
        with suppress(asyncio.CancelledError):
            await asyncio.gather(client_task, vl_task, return_exceptions=True)
        with suppress(Exception):
            await websocket.close()


# ---------------------------------------------------------------------------
# Consent
# ---------------------------------------------------------------------------


@router.post("/consent", status_code=201)
async def record_consent(
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    disclosure_version: str = CURRENT_DISCLOSURE_VERSION,
) -> dict[str, Any]:
    """Record offshore-inference consent for the calling tenant (FR-053d).

    ``consenting_identity_object_id``/``display_name`` come from the validated token (FR-007),
    never a caller-supplied value or a fabricated UUID — the consent artefact must actually
    identify who consented. Also attaches the recorded consent to the tenant's own record via
    :func:`attach_offshore_inference_consent`, which is the field
    :class:`~groundwork_channels.voice.enablement.VoiceEnablementGate` actually reads — recording
    the artefact alone does not enable anything.
    """
    await _require_voice(request)
    if disclosure_version != CURRENT_DISCLOSURE_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                "disclosureVersion does not match the currently published offshore-inference "
                f"disclosure version {CURRENT_DISCLOSURE_VERSION!r}"
            ),
        )
    consent_store: OffshoreInferenceConsentStore = request.app.state.consent_store
    consent_id = str(uuid.uuid4())
    consent: OffshoreInferenceConsent = await consent_store.record(
        consent_id=consent_id,
        consenting_identity_object_id=caller.object_id,
        consenting_identity_display_name=caller.display_name,
        disclosure_version=disclosure_version,
        now=_now(request),
    )
    await attach_offshore_inference_consent(
        tenant_id=caller.tenant_id, consent=consent, request=request
    )
    return consent.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Enablement
# ---------------------------------------------------------------------------


@router.get("/auth-config")
async def get_auth_config(request: Request) -> dict[str, str]:
    """The Entra app this environment's frontend must sign in against (FR-006 sibling).

    Deliberately pre-authentication and unauthenticated — a browser needs this before it can
    acquire a token at all, and a client ID / audience are not secrets (they are the same values
    visible in any browser's network tab or the app registration blade). Exists so
    ``voice.html`` never hardcodes a specific environment's Entra app: a client ID baked into
    the page is wrong the moment the page is served from a different environment than the one
    it was authored against — exactly what happened before this endpoint existed.
    """
    entra = request.app.state.settings.entra
    return {"clientId": entra.client_id, "scope": f"{entra.audience}/access_as_user"}


class _EnablementRateLimiter:
    """Sliding-window per-client rate limit for the deliberately pre-authentication
    ``GET /enablement/{tenant_id}`` route.

    That route must stay pre-auth (a caller checks availability before a token exists), which is
    exactly what makes tenant-id enumeration possible there — the disclosed gap recorded on the
    route's own docstring. This limiter does not close that gap (any slow scraper still gets an
    answer eventually); it converts "free enumeration" into "rate-billed enumeration", which is
    the cheap mitigation until a signed-enablement-link design replaces it. Instances are
    per-app via ``app.state`` so contract tests get isolation for free.
    """

    def __init__(
        self,
        *,
        max_requests: int = 20,
        window_seconds: float = 60.0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._max_requests = max_requests
        self._window_seconds = window_seconds
        self._now_fn = now_fn or (lambda: datetime.now(UTC))
        self._hits: dict[str, deque[datetime]] = {}

    def check(self, client_host: str) -> None:
        now = self._now_fn()
        window_start = now - timedelta(seconds=self._window_seconds)
        hits = self._hits.setdefault(client_host, deque())
        while hits and hits[0] <= window_start:
            hits.popleft()
        if len(hits) >= self._max_requests:
            raise HTTPException(status_code=429, detail="too many enablement checks; retry later")
        hits.append(now)


@router.get("/enablement/{tenant_id}")
async def check_enablement(request: Request, tenant_id: str) -> dict[str, Any]:
    """Check whether voice is available for a tenant (FR-053e).

    Deliberately pre-authentication — a caller needs to know whether voice is even available
    before a token exists to present. Read-only, no tenant data returned beyond a boolean and a
    reason string. **Disclosed, narrowed not closed**: this still allows enumerating arbitrary
    tenant IDs' enablement status, but ``_EnablementRateLimiter`` bills attempts per client so
    bulk scraping is no longer free. A signed-enablement-link design would close it entirely;
    that remains a deliberate future scope decision.
    """
    limiter: _EnablementRateLimiter | None = getattr(request.app.state, "enablement_limiter", None)
    if limiter is None:
        # Contract-test apps built without full startup wiring keep working; production always
        # configures one in api/main.py.
        limiter = _EnablementRateLimiter()
    client_host = request.client.host if request.client is not None else "unknown"
    limiter.check(client_host)
    await _require_voice(request)
    gate: VoiceEnablementGate = request.app.state.voice_gate
    result = await gate.check(tenant_id)
    return {"mayAccept": result.may_accept, "reason": result.reason}


# ---------------------------------------------------------------------------
# Conversational chat — multi-turn with plan generation
# ---------------------------------------------------------------------------


_PROMPTS_DIR = Path(__file__).parent / "prompts"
# Structured per the agent-instruction-design skill: identity, scope, behaviour, workflow, tool
# contract, security - minimum sufficient set for a voice channel (one or two spoken sentences
# per turn), with accuracy explicitly outranking brevity. Full text in
# prompts/voice_conversation.md, not inline here, matching planning.py's own prompt-extraction
# pattern (see hardening.py and planning.py) - `.rstrip("\n")` because the file, unlike the
# original inline string literal it replaces, ends with a trailing newline.
_VOICE_CONVERSATION_INSTRUCTIONS = (
    (_PROMPTS_DIR / "voice_conversation.md").read_text(encoding="utf-8").rstrip("\n")
)
_CONVERSATION_SYSTEM = PROMPT_HARDENING_PREAMBLE + _VOICE_CONVERSATION_INSTRUCTIONS


def _voice_conversation_summary(
    blueprint: Any,
    *,
    subscription_id: str,
    region: str,
    fabric_capacity_sku: str,
    notification_email: str | None = None,
) -> str:
    """Render tool-extracted parameters as the prompt text ``PlanningAgent.generate_plan``
    expects — the same adapter role ``plans.py``'s ``CreatePlanRequest.conversation_summary``
    plays for the structured, non-conversational path."""
    email_clause = f" Notification email: {notification_email}." if notification_email else ""
    return (
        f"Deploy the {blueprint.display_name!r} blueprint "
        f"(blueprint_id={blueprint.blueprint_id}, version={blueprint.version}) into "
        f"subscription {subscription_id}, region {region}, environment production. "
        f"The customer has explicitly requested Fabric capacity SKU {fabric_capacity_sku}."
        f"{email_clause}"
    )


async def _persist_engagement_details(
    conn: Conn,
    *,
    tenant_id: str,
    email: str | None = None,
    devops_organization_url: str | None = None,
    fabric_capacity_admin_upn: str | None = None,
) -> str:
    """Persist conversation-confirmed engagement details to the ``CustomerTenant`` record.

    The organization URL is *verified before it is recorded*: one authenticated
    ``connectionData`` probe (``probe_organization_status`` — the same probe the readiness check
    uses) distinguishes a real organization from a typo'd one. Only ``200``/``401``/``403``
    persist — the org exists in all three; ``401``/``403`` mean Groundwork still needs to be
    added as an organization user, which plan-time readiness reports properly. A ``404`` (or
    anything unexpected) refuses the write and the returned verdict tells the calling tool path,
    so the model can tell the customer the URL looks wrong instead of the record silently
    holding a value every later stage would fail against.

    Uses ETag-based optimistic concurrency (up to three attempts) so a concurrent tenant update
    is not silently overwritten. Best-effort: logs and continues if the tenant cannot be read or
    the values are already set to the same values.

    Returns the organization verdict (``"verified"`` | ``"exists_access_pending"`` |
    ``"not_found"`` | ``"error"`` | ``"skipped"``) so the tool result can carry it back.
    """
    from azure.cosmos.exceptions import CosmosHttpResponseError

    from groundwork_shared.validation.checks.devops import probe_organization_status

    org_verdict = "skipped"
    if devops_organization_url is not None:
        try:
            status = await probe_organization_status(
                devops_organization_url, conn.app.state.credential
            )
            if status == 200:
                org_verdict = "verified"
            elif status in (401, 403):
                org_verdict = "exists_access_pending"
            else:
                # 404 (no such organization) or any unexpected status: the URL is not one this
                # platform can record as the customer's organization.
                logger.warning(
                    "persist_engagement_details: organization URL probe returned %s for tenant "
                    "%s - not persisting",
                    status,
                    tenant_id,
                )
                devops_organization_url = None
                org_verdict = "not_found" if status == 404 else "error"
        except Exception as exc:
            logger.warning(
                "persist_engagement_details: organization probe failed for tenant %s: %s",
                tenant_id,
                exc,
            )
            devops_organization_url = None
            org_verdict = "error"

    if email is None and devops_organization_url is None and fabric_capacity_admin_upn is None:
        return org_verdict

    tenant_repository = conn.app.state.tenant_repository
    _MAX_RETRIES = 3
    for attempt in range(_MAX_RETRIES):
        result = await tenant_repository.read_with_etag(tenant_id, tenant_id)
        if result is None:
            logger.warning("persist_engagement_details: tenant %s not found - skipping", tenant_id)
            return org_verdict
        tenant, etag = result
        update: dict[str, str] = {}
        if email is not None and tenant.notification_email != email:
            update["notification_email"] = email
        if devops_organization_url is not None and tenant.devops_organization_url != (
            devops_organization_url
        ):
            update["devops_organization_url"] = devops_organization_url
        if fabric_capacity_admin_upn is not None and tenant.fabric_capacity_admin_upn != (
            fabric_capacity_admin_upn
        ):
            update["fabric_capacity_admin_upn"] = fabric_capacity_admin_upn
        if not update:
            return org_verdict  # Already set; nothing to do.
        updated = tenant.model_copy(update=update)
        try:
            await tenant_repository.replace_with_etag(tenant_id, updated, etag=etag)
            return org_verdict
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412 or attempt == _MAX_RETRIES - 1:
                logger.warning(
                    "persist_engagement_details: failed to update tenant %s: %s",
                    tenant_id,
                    exc,
                )
                return org_verdict
    # All retries lost the ETag race to a concurrent writer; the next conversation turn (or the
    # operator) will re-attempt. Not an error worth surfacing beyond the log above.
    return org_verdict


def _new_conversation_record(
    conn: Conn, *, caller: AuthenticatedCaller, session_id: str, now: datetime
) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=session_id,
        tenant_id=caller.tenant_id,
        correlation_id=session_id,
        channel=ConversationChannel.VOICE,
        locale="en-AU",
        storage_region=conn.app.state.settings.residency.storage_region,
        retention_expires_at=now + CONVERSATION_RETENTION,
        created_at=now,
    )


def _append_turn(
    record: ConversationRecord,
    *,
    speaker: str,
    text: str,
    occurred_at: datetime,
    recognition_confidence: float | None = None,
) -> ConversationRecord:
    updated = record.model_copy(
        update={
            "transcript": (
                *record.transcript,
                ConversationTurn(
                    sequence=len(record.transcript),
                    speaker=speaker,
                    text=text,
                    occurred_at=occurred_at,
                    recognition_confidence=recognition_confidence,
                ),
            )
        }
    )
    return ConversationRecord.model_validate(updated.model_dump())


async def _load_conversation(
    conn: Conn, *, caller: AuthenticatedCaller, session_id: str
) -> tuple[ConversationRecord, bool, str | None]:
    """Load an existing conversation or create a new one.

    Returns ``(record, existed, etag)`` where ``etag`` is the Cosmos ETag from the read, or
    ``None`` for a new (not-yet-persisted) record. Pass the ETag to :func:`_save_conversation`
    so it can use optimistic concurrency on the update.
    """
    repository = conn.app.state.conversation_repository
    result = await repository.read_with_etag(caller.tenant_id, session_id)
    if result is None:
        return (
            _new_conversation_record(conn, caller=caller, session_id=session_id, now=_now(conn)),
            False,
            None,
        )
    record, etag = result
    return record, True, etag


async def _save_conversation(
    conn: Conn, *, record: ConversationRecord, existed: bool, etag: str | None = None
) -> ConversationRecord:
    """Persist a conversation record.

    For existing records, uses ETag-based optimistic concurrency: if ``etag`` is supplied, the
    Cosmos write is conditional on the document not having changed since it was read. On a 412
    conflict (another writer modified the same session concurrently), re-reads the latest version,
    re-applies the most recently appended turn, and retries — up to three times before giving up.
    """
    from azure.cosmos.exceptions import CosmosHttpResponseError

    repository = conn.app.state.conversation_repository
    if not existed:
        return await repository.create(record.tenant_id, record)

    _MAX_RETRIES = 3
    current_etag = etag
    current_record = record
    for attempt in range(_MAX_RETRIES):
        try:
            return await repository.replace_with_etag(
                record.tenant_id, current_record, etag=current_etag
            )
        except CosmosHttpResponseError as exc:
            if exc.status_code != 412 or attempt == _MAX_RETRIES - 1:
                raise
            # Concurrent modification: re-read the latest version, re-apply the new turns that
            # the current_record has beyond what was in the base, then retry.
            latest_result = await repository.read_with_etag(
                record.tenant_id, record.conversation_id
            )
            if latest_result is None:
                # Document disappeared between our read and now — treat as new.
                return await repository.create(record.tenant_id, current_record)
            latest, current_etag = latest_result
            # Re-apply turns added after the base that the latest doesn't have yet.
            base_len = len(latest.transcript)
            new_turns = current_record.transcript[base_len:]
            merged = latest
            for turn in new_turns:
                merged = _append_turn(
                    merged,
                    speaker=turn.speaker,
                    text=turn.text,
                    occurred_at=turn.occurred_at,
                    recognition_confidence=turn.recognition_confidence,
                )
            current_record = merged
    # Unreachable (loop always returns or raises), but satisfies the type checker.
    return await repository.replace(record.tenant_id, current_record)


def _conversation_messages(record: ConversationRecord) -> list[dict[str, str]]:
    messages: list[dict[str, str]] = [{"role": "system", "content": _CONVERSATION_SYSTEM}]
    for turn in record.transcript:
        role = "user" if turn.speaker == "customer" else "assistant"
        messages.append({"role": role, "content": turn.text})
    return messages


async def _seal_plan_for_caller(
    conn: Conn, caller: AuthenticatedCaller, plan: DeploymentPlan
) -> dict[str, Any]:
    """Entitlement-check, seal, and persist a plan the planning agent already produced.

    The shared tail of both ``/plan`` (free-text transcript), ``/chat``'s tool-call path, and
    the Voice Live WebSocket's ``generate_plan`` tool — a plan is only ever handed back to a
    voice caller once it is a real, persisted ``SealedDeploymentPlan`` a later ``/approve`` call
    can reference by its actual hash. This is what the pre-fix version of this module skipped
    entirely (see the module docstring).
    """
    tenant = await get_customer_tenant(conn, caller.tenant_id)
    if not tenant.consent_state.permits_tenant_operations:
        # FR-006: same check api/plans.py's REST path already makes — found live 2026-08-24 that
        # this shared tail (voice, /plan, /chat) never made it at all, so a voice caller could
        # seal a plan for a tenant with no Lighthouse delegation granted, inconsistent with the
        # REST path's own enforcement.
        raise AuthorizationError(
            f"tenant consent state is {tenant.consent_state.value!r}, not granted"
        )
    if tenant.entitlement_for(plan.subscription_id) is None:
        raise AuthorizationError(f"tenant is not entitled to subscription {plan.subscription_id}")
    if plan.region.value not in tenant.approved_regions:
        raise HTTPException(
            status_code=409,
            detail=f"region {plan.region.value!r} is outside this tenant's approved region set",
        )

    sealed = seal_plan(
        plan,
        tenant_id=caller.tenant_id,
        requesting_identity_object_id=caller.object_id,
        requesting_channel="voice",
    )
    plan_repository = conn.app.state.plan_repository
    created = await plan_repository.create(caller.tenant_id, sealed)

    return {
        "planId": created.plan_hash,
        "planHash": created.plan_hash,
        "blueprintId": created.plan.blueprint_id,
        "region": created.plan.region.value,
        "fabricCapacitySku": created.plan.fabric_capacity_sku.value,
        "resources": [
            {"resourceType": r.resource_type, "logicalName": r.logical_name}
            for r in created.plan.resource_set
        ],
        # Hand-mapped to camelCase, same as every other key in this dict — found live
        # 2026-08-24: `.model_dump(mode="json")` alone dumps Pydantic's snake_case field names
        # (monthly_total, ...), which the frontend's cost.monthlyTotal read as `undefined` -
        # displayed as "undefined AUD/month" and, more seriously, sent as the approve request's
        # own acknowledgedCostAud, which would have failed real cost re-verification too.
        "costEstimate": {
            "currency": created.plan.cost_estimate.currency,
            "monthlyTotal": created.plan.cost_estimate.monthly_total,
            "uncertaintyLowerPct": created.plan.cost_estimate.uncertainty_lower_pct,
            "uncertaintyUpperPct": created.plan.cost_estimate.uncertainty_upper_pct,
            "basis": created.plan.cost_estimate.basis,
        },
        # FR-013d: the disclosure the frontend must render (and gate its Approve button on)
        # for a below-F64 plan — the voice payload carried only a boolean before, which is a
        # flag, not a shown statement.
        "requiresPowerBiViewerLicensing": created.plan.requires_licensing_disclosure(),
        "licensingDisclosure": licensing_disclosure_for(created.plan.fabric_capacity_sku),
        "validityWindow": {
            "notBefore": created.validity.not_before.isoformat(),
            "notAfter": created.validity.not_after.isoformat(),
        },
        "status": "ready_for_approval",
    }


@router.post("/chat")
async def voice_chat(
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    message: Annotated[str, "message"] = "",
    session_id: Annotated[str, "session-id"] = "",
) -> dict[str, Any]:
    """Conversational voice agent — multi-turn with tool-calling for planning and onboarding.

    Text counterpart to the WS relay's function-call handling, sharing the same tool schemas and
    the same real ``_run_*_tool`` implementations — only the model-call mechanism differs (a
    request/response round trip here, an event stream there). The model's tool calls only
    *extract* parameters from natural conversation (the deterministic-execution boundary: model
    output must become a schema-validated object, never the orchestration object itself) —
    ``generate_plan``'s real
    plan is built by the identical ``PlanningAgent.generate_plan`` → ``seal_plan`` →
    ``plan_repository`` pipeline every other channel uses, via ``_seal_plan_for_caller`` above.
    """
    await _require_voice(request)

    if not message.strip():
        raise HTTPException(status_code=400, detail="message must not be empty")

    planning_agent: PlanningAgent | None = getattr(request.app.state, "planning_agent", None)
    if planning_agent is None:
        raise HTTPException(status_code=501, detail="Planning agent not wired on app.state.")

    # Validate or generate the session ID. A caller-supplied ID must be a UUID; anything else
    # is rejected with 400 rather than letting a Pydantic ValidationError surface as a 500.
    raw_sid = session_id.strip()
    if raw_sid:
        try:
            uuid.UUID(raw_sid)
        except ValueError as exc:
            raise HTTPException(
                status_code=400,
                detail="session_id must be a UUID (e.g. '11111111-1111-1111-1111-111111111111')",
            ) from exc
        sid = raw_sid
    else:
        sid = str(uuid.uuid4())

    conversation, existed, etag = await _load_conversation(request, caller=caller, session_id=sid)
    conversation = _append_turn(
        conversation, speaker="customer", text=message.strip(), occurred_at=_now(request)
    )
    conversation = await _save_conversation(
        request, record=conversation, existed=existed, etag=etag
    )

    # Same tool set the WS relay uses — the flat Voice Live shape works unchanged against the
    # native client's /responses route too (verified live 2026-09-06), so there is exactly one
    # set of tool schemas for both surfaces, not two that could drift apart.
    tools = [
        _GENERATE_PLAN_TOOL_VOICE_LIVE,
        _GET_ONBOARDING_STATUS_TOOL_VOICE_LIVE,
        _CREATE_TENANT_TOOL_VOICE_LIVE,
        _CONFIRM_CUSTOMER_CONSENT_TOOL_VOICE_LIVE,
        _GRANT_ADO_ORG_ACCESS_TOOL_VOICE_LIVE,
        _CHECK_PLAN_STATUS_TOOL_VOICE_LIVE,
        _TRIGGER_BOOTSTRAP_IDENTITY_TOOL_VOICE_LIVE,
        _QUICK_ONBOARD_TOOL_VOICE_LIVE,
    ]

    messages = _conversation_messages(conversation)
    af_messages = [Message(role=m["role"], contents=[m["content"]]) for m in messages]
    client = request.app.state.voice_tool_client
    try:
        response = await client.get_response(
            af_messages,
            options={
                "tools": tools,
                "tool_choice": "auto",
                "temperature": 0.3,
                "max_tokens": 200,
            },
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Model call failed: {exc}") from exc

    # Tool call path — model wants to generate a plan or run an onboarding step. The model only
    # ever supplies the extracted parameters; the real DeploymentPlan (resource set, cost
    # estimate, risk assessment) comes from the same PlanningAgent.generate_plan the structured
    # /plans route uses, and is only ever returned once sealed and persisted
    # (_seal_plan_for_caller). Each other tool call runs the identical real business-logic
    # function the WS relay's execute_pending_call dispatches to — one implementation per tool,
    # two call sites.
    plan: dict[str, Any] | None = None
    onboarding: dict[str, Any] | None = None
    function_calls = [c for c in response.messages[-1].contents if c.type == "function_call"]
    for call in function_calls:
        args = json.loads(call.arguments or "{}")
        if call.name == "generate_plan":
            blueprint = get_blueprint(_VOICE_BLUEPRINT_ID, request)
            notification_email: str | None = args.get("notification_email") or None
            summary = _voice_conversation_summary(
                blueprint,
                subscription_id=args["subscription_id"],
                region=args["region"],
                fabric_capacity_sku=args["fabric_capacity_sku"],
                notification_email=notification_email,
            )
            generated = await planning_agent.generate_plan(summary)
            plan = await _seal_plan_for_caller(request, caller, generated)

            # Persist the confirmed email and (verified) organization URL to the tenant
            # record (FR-002 / FR-051), ETag-guarded so a concurrent update is not lost.
            # The org verdict rides on the returned plan dict for the frontend to surface.
            org_verdict = await _persist_engagement_details(
                request,
                tenant_id=caller.tenant_id,
                email=notification_email,
                devops_organization_url=args.get("devops_organization_url") or None,
                fabric_capacity_admin_upn=args.get("fabric_capacity_admin_upn") or None,
            )
            plan = {**plan, "devops_organization_check": org_verdict}
        elif call.name == "get_onboarding_status":
            onboarding = await _run_get_onboarding_status_tool(request, caller)
        elif call.name == "create_tenant":
            onboarding = await _run_create_tenant_tool(request, caller, args)
        elif call.name == "confirm_customer_consent":
            onboarding = await _run_confirm_customer_consent_tool(request, caller, args)
        elif call.name == "grant_ado_org_access":
            onboarding = await _run_grant_ado_org_access_tool(request, caller, args)
        elif call.name == "check_plan_status":
            onboarding = await _run_check_plan_status_tool(request, caller, args)
        elif call.name == "trigger_bootstrap_identity":
            onboarding = await _run_trigger_bootstrap_identity_tool(request, caller, args)
        elif call.name == "quick_onboard":
            onboarding = await _run_quick_onboard_tool(request, caller, args)

    # Text reply path
    reply = response.text or ""
    if not reply and plan:
        reply = (
            f"Plan ready: {plan['blueprintId']}, {plan['region']}, "
            f"{plan['fabricCapacitySku']}. Ready for approval."
        )
    if reply:
        conversation = _append_turn(
            conversation, speaker="agent", text=reply, occurred_at=_now(request)
        )
        # etag=None: the first save in this request already persisted the customer turn;
        # the agent reply is a second sequential write within the same request so there is
        # no concurrent-modification risk from another request writing between the two saves.
        await _save_conversation(request, record=conversation, existed=True, etag=None)

    return {
        "sessionId": sid,
        "reply": reply,
        "plan": plan,
        "onboarding": onboarding,
    }


# ---------------------------------------------------------------------------
# Voice-to-plan bridge (one-shot — kept for direct plan-last-turn use)
# ---------------------------------------------------------------------------


@router.post("/plan")
async def voice_to_plan(
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    transcript: Annotated[str, "transcript"] = "",
) -> dict[str, Any]:
    """Accept a text transcript and return a real, sealed deployment plan — one-shot, no
    conversation. Entitlement/region checks apply to whatever subscription and region the model
    actually extracted from the transcript (free text names them, there is no caller-supplied
    structured field to pre-check before generation, unlike ``/plans``)."""
    await _require_voice(request)

    if not transcript.strip():
        raise HTTPException(status_code=400, detail="transcript must not be empty")

    planning_agent: PlanningAgent | None = getattr(request.app.state, "planning_agent", None)
    if planning_agent is None:
        raise HTTPException(status_code=501, detail="Planning agent not wired on app.state.")

    session_id = str(uuid.uuid4())
    conversation = _new_conversation_record(
        request, caller=caller, session_id=session_id, now=_now(request)
    )
    conversation = _append_turn(
        conversation, speaker="customer", text=transcript.strip(), occurred_at=_now(request)
    )
    conversation = await _save_conversation(request, record=conversation, existed=False)
    plan = await planning_agent.generate_plan(transcript)
    sealed = await _seal_plan_for_caller(request, caller, plan)
    conversation = _append_turn(
        conversation,
        speaker="agent",
        text=(
            f"Plan ready: {sealed['blueprintId']}, {sealed['region']}, "
            f"{sealed['fabricCapacitySku']}. Ready for approval."
        ),
        occurred_at=_now(request),
    )
    await _save_conversation(request, record=conversation, existed=True, etag=None)
    return sealed


# ---------------------------------------------------------------------------
# Voice approval — real approval service, then queue deployment
# ---------------------------------------------------------------------------


class VoiceApprovalRequest(BaseModel):
    """``{ planHash, acknowledgedCostAud }`` — the voice-channel counterpart of
    ``approvals.py``'s ``CreateApprovalRequest``, minus ``channel`` (always ``VOICE`` here).
    No ``subscriptionId``/``region``/``fabricSku``: those come from the *plan* the approval
    references, never from a value repeated back over the call — a caller-supplied value here
    could disagree with what was actually planned and priced.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    plan_hash: Annotated[str, Field(alias="planHash", pattern=r"^sha256:[0-9a-f]{64}$")]
    acknowledged_cost_aud: Annotated[float, Field(alias="acknowledgedCostAud", ge=0)]
    acknowledged_powerbi_viewer_licensing: Annotated[
        bool, Field(alias="acknowledgedPowerBiViewerLicensing")
    ] = False
    """FR-013d, required ``true`` below F64 — the voice counterpart of ``approvals.py``'s own
    field; the frontend renders the plan's ``licensingDisclosure`` and gates the Approve
    button on its acknowledgement."""


@router.post("/approve")
async def voice_approve(
    request: Request,
    body: VoiceApprovalRequest,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, Any]:
    """Record a voice approval against a real, previously-sealed plan, then queue the deployment.

    ADR-0011 lets a spoken agreement alone authorise execution — the channel-durability
    requirement is gone — but every other guarantee this codebase enforces for every other channel
    still applies here: the caller must present a validated token, the approval is bound to a real
    plan by its actual ``plan_hash`` (not a fabricated one), cost is re-verified live against that
    plan (FR-019), and an above-threshold plan still requires a second, distinct approver (SC-018)
    — a first voice approval above threshold returns ``secondApprovalRequired: true`` and queues
    nothing yet, exactly like ``POST /plans/{planId}/approvals``.

    Deployment admission and queuing go through ``queue_deployment_for_approval``
    (``api/deployments.py``) — the identical function ``POST /deployments`` calls — so a
    voice-originated deployment gets the same tenant concurrency check, the same
    ``AuthorityChain``, and picks up in the orchestrator's queue-consumption loop the same way.
    """
    await _require_voice(request)
    caller.require_role(CallerRole.APPROVER)

    plan_repository = request.app.state.plan_repository
    sealed = await plan_repository.read(caller.tenant_id, body.plan_hash)
    if sealed is None:
        raise HTTPException(status_code=404, detail="plan not found")

    existing = await find_approval_by_plan_hash(
        pending_repository=request.app.state.pending_approval_repository,
        approval_repository=request.app.state.approval_repository,
        tenant_id=caller.tenant_id,
        plan_hash=sealed.plan_hash,
    )

    record = await record_approval(
        sealed_plan=sealed,
        existing=existing,
        caller=caller,
        plan_hash=body.plan_hash,
        acknowledged_cost_aud=body.acknowledged_cost_aud,
        channel=ApprovalChannel.VOICE,
        threshold=_threshold_policy(request),
        retail_prices_client=request.app.state.retail_prices_client,
        artefact_store=request.app.state.approval_artefact_store,
        now=_now(request),
        acknowledged_powerbi_viewer_licensing=body.acknowledged_powerbi_viewer_licensing,
        require_step_up_approval=request.app.state.settings.governance.require_step_up_approval,
    )

    if isinstance(record, PendingApproval):
        await request.app.state.pending_approval_repository.replace(caller.tenant_id, record)
        return {
            "approvalId": record.approval_id,
            "planHash": record.plan_hash,
            "secondApprovalRequired": True,
            "status": "awaiting_second_approval",
        }

    await request.app.state.approval_repository.replace(caller.tenant_id, record)
    deployment = await queue_deployment_for_approval(request, caller, record)

    return {
        "approvalId": record.approval_id,
        "planHash": record.plan_hash,
        "secondApprovalRequired": False,
        "status": "queued",
        "deploymentId": deployment.deployment_id,
        "subscriptionId": deployment.subscription_id,
    }
