"""Voice confirmation recording — constructs an Approval from a spoken agreement (T106).

As of 2026-08-02 (see ADR-0011), spoken agreement alone MAY authorise an irreversible
action at any cost/threshold — the prior FR-023 prohibition is removed by explicit product-owner
decision. This module constructs and records an ``Approval`` / ``SecondApproval`` with
``channel=VOICE`` directly from the call, rather than handing off to another channel.

**App consent and caller identity are separate concerns.** Organisation-wide multi-tenant app
consent (see ``api/tenants.py``) authenticates the *consenting operator* who registered the
Groundwork application in the tenant — it does not authenticate the PSTN caller who dials in
later. A caller can reach this point only because an authorised operator set up app consent in
advance; that consent establishes the *tenant's* authority, not the *caller's* identity.

**Caller-identity gap (see ADR-0011 — OPEN, not resolved):** Caller ID
from a PSTN call is not authentication. ``CallerContext.identity_object_id`` is ``None`` for
normal PSTN calls, meaning only caller ID (a spoofable string) is available to tie the approval
to a person. Resolving this gap requires a binding between the call leg and a verified Entra
identity — nothing in this codebase implements that yet. The approval record discloses that
``identity_object_id`` is absent; the gap is tracked in ``AGENT_HANDOFF.md`` and in ADR-0011.
Do not interpret app consent as solving this.

**Caller persona**: ``CallerContext.display_name`` is the name/persona captured during the call
(gathered via ``ClarificationTracker.CALLER_DISPLAY_NAME`` or from the telephony caller-ID string).
It is used in the approval record as ``approving_identity_display_name``. When
``identity_object_id`` is ``None`` (the normal PSTN case), the display name doubles as the object
id in the approval record — this is intentional and disclosed, not a fabricated strong-identity
proof.

**What this module actually does**: accepts a ``CallerContext`` (whatever identity the telephony
handler was able to resolve), a ``plan_hash``, and an ``ApprovalServiceLike`` (the same
``approval/service.py`` the chat channel already uses — voice just calls it from a different entry
point). It constructs the approval with ``channel=VOICE`` and returns the result. Nothing here
reaches into the planning agent — the caller is responsible for having already obtained the plan
details and cost acknowledgement from the voice conversation itself (``voicelive.py`` → planning
agent, same path as chat).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from groundwork_contracts.approval import ApprovalChannel
from groundwork_contracts.tenant import ConversationChannel

_VOICE_ALONE_AUTHORISATION_DECISION = "ADR-0011 (2026-08-02) — voice alone MAY authorise execution"


class ApprovalServiceLike(Protocol):
    """The subset of ``approval/service.py`` this module depends on — injectable for testing."""

    async def record_approval(
        self,
        *,
        plan_hash: str,
        approving_identity_object_id: str,
        approving_identity_display_name: str,
        channel: ApprovalChannel,
        cost_estimate: Any,
        threshold_applied: Any,
        approved_parameters: Any,
        now: datetime,
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class CallerContext:
    """What we know about the caller. ``display_name`` is the raw caller ID string —
    NOT an authenticated identity. The ``identity_object_id`` is ``None`` when caller ID
    is the only signal available (which is the normal case for PSTN calls)."""

    display_name: str
    channel: ConversationChannel = ConversationChannel.VOICE
    identity_object_id: str | None = None
    """``None`` means we only have caller ID, not an authenticated Entra identity. The approval
    service will still accept this (voice is the primary entry point), but the gap is disclosed —
    see the module docstring."""


class VoiceHandoffError(Exception):
    """The approval service refused the voice confirmation (e.g. expired plan, hash mismatch)."""


class VoiceHandoff:
    """Constructs an ``Approval`` with ``channel=VOICE`` from a spoken agreement and records
    it through the same ``approval/service.py`` the chat channel already uses.

    ``approval_service`` is an :class:`ApprovalServiceLike` — in production this is the real
    ``approval/service.py`` module; in tests it is a fake with no Cosmos/blob behind it.
    """

    def __init__(self, approval_service: ApprovalServiceLike) -> None:
        self._approval_service = approval_service

    async def confirm_and_approve(
        self,
        *,
        caller: CallerContext,
        plan_hash: str,
        cost_estimate: Any,
        threshold_applied: Any,
        approved_parameters: Any,
        now: datetime,
    ) -> Any:
        """Record a voice approval. Returns the constructed ``Approval`` object from the service.

        ``caller.identity_object_id`` may be ``None`` (caller ID only — the normal PSTN case)
        — the approval service is responsible for accepting or rejecting that, not this module.
        """
        try:
            return await self._approval_service.record_approval(
                plan_hash=plan_hash,
                approving_identity_object_id=caller.identity_object_id or caller.display_name,
                approving_identity_display_name=caller.display_name,
                channel=ApprovalChannel.VOICE,
                cost_estimate=cost_estimate,
                threshold_applied=threshold_applied,
                approved_parameters=approved_parameters,
                now=now,
            )
        except Exception as exc:
            raise VoiceHandoffError(
                f"voice approval recording failed for plan {plan_hash[:16]}...: {exc}"
            ) from exc
