"""Customer tenant onboarding — FR-006, operator-driven.

Real, multi-tenant admin consent — a customer's own Entra administrator visiting Microsoft's own
consent endpoint and approving the application's permissions in *their* tenant — is the only way
authority into a customer tenant is ever established. This module never invents authority any
other way: ``CustomerTenant.consent_state`` starts ``PENDING`` on creation and there is no route
here that can construct one already ``GRANTED``.

**Consent verification is operator attestation, not an automated callback or a standing
credential — a direct, disclosed decision (2026-08-06), not a stub.** Two "fully automatic"
designs were considered and rejected:

1. A browser-redirect callback hosted by this API, trusting the ``tenant``/``admin_consent`` query
   parameters Entra appends to the redirect. Microsoft's own documentation
   (learn.microsoft.com/entra/identity-platform/v2-admin-consent, `[VERIFIED]` 2026-08-06)
   explicitly warns: "Never use the tenant ID value of the `tenant` parameter to authenticate or
   authorize users... can cause your application to be exposed to security incidents" — anyone can
   forge that redirect by just navigating to the URL with fabricated parameters. Building a
   callback that trusted it would be exactly the kind of fabricated verification this platform
   forbids — a check must be real or the gap must be disclosed, never assumed. Separately,
   `controlplane` has no internet-facing endpoint today (`ClusterIP` only) —
   hosting a real callback is a new public attack surface with its own security review, out of
   scope for onboarding alone.
2. A server-to-server client-credentials check against the customer's tenant (attempt a token
   acquisition; success proves consent was granted). This needs the application to hold a
   certificate or secret credential — in tension with this project's own secretless principle (no
   long-lived client credentials, ever), even though a certificate is more defensible than a
   plaintext secret.

Instead: the admin-consent URL still sends the customer's admin through the real Microsoft consent
flow (``GET /v1/tenants/{tenantId}/onboarding/consent-url``), landing them on a generic, already-
hosted Microsoft page afterwards (``Settings.entra.redirect_uri`` — informational only, never a
verification signal). Separately, a Groundwork operator — a real, authenticated
``CallerRole.OPERATOR`` identity — confirms consent was actually granted, by checking the
customer's own Entra admin center or the customer confirming directly, and that confirmation is
what flips ``consent_state`` to ``GRANTED`` (``POST
/v1/tenants/{tenantId}/onboarding/confirm``). Honest and fully auditable — the confirming
identity is recorded directly on the tenant record (``CustomerTenant.consent_confirmed_by_*``) —
but not self-service for the customer. Revisit if/when a public ingress or a certificate-based
credential becomes acceptable.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Protocol
from urllib.parse import urlencode

import httpx
from azure.cosmos.exceptions import CosmosResourceExistsError
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from groundwork_channels.voice.consent import (
    CURRENT_DISCLOSURE_VERSION,
    OFFSHORE_INFERENCE_DISCLOSURE,
    OffshoreInferenceConsentStore,
)
from groundwork_contracts.tenant import (
    AdoOrgAccessState,
    ConsentState,
    CustomerTenant,
    OffshoreInferenceConsent,
    SubscriptionEntitlement,
)
from groundwork_controlplane.api.auth import AuthenticatedCaller, CallerRole
from groundwork_controlplane.api.lighthouse_onboarding import (
    DelegationState,
    build_azure_devops_instructions,
    build_tenant_onboarding_facts,
)
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_orchestrator.stages.identity import (
    CONNECTION_DATA_API_VERSION,
    FEDERATED_CREDENTIAL_API_VERSION,
    TOKEN_EXCHANGE_AUDIENCE,
    _expected_subject,
    federated_credential_name,
    federated_credential_resource_id,
    managed_identity_name,
)
from groundwork_orchestrator.stages.infrastructure import (
    ARM_ENDPOINT,
    ARM_SCOPE,
    deployment_resource_group_name,
)
from groundwork_orchestrator.stages.pipeline_execution import AZURE_DEVOPS_RESOURCE_ID
from groundwork_orchestrator.state.repositories import CustomerTenantRepository
from groundwork_shared.notify.dispatcher import NotificationDispatchError, build_email_sender
from groundwork_shared.telemetry.scrubbing import scrub_text
from groundwork_shared.validation.checks.devops import probe_organization_status

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])

logger = logging.getLogger(__name__)


class TokenCredentialLike(Protocol):
    async def get_token(self, *scopes: str, **kwargs: object) -> object: ...


class TenantExistence(StrEnum):
    """Outcome of asking Entra's own OIDC discovery endpoint whether a tenant exists."""

    EXISTS = "exists"
    NOT_FOUND = "not_found"
    INDETERMINATE = "indeterminate"


def evaluate_tenant_existence(status_code: int, body: str) -> TenantExistence:
    """Interpret one ``GET .../v2.0/.well-known/openid-configuration`` response.

    ``[VERIFIED]`` 2026-08-22, live against login.microsoftonline.com: a real tenant returns
    ``200`` with OIDC metadata; a well-formed but nonexistent tenant GUID returns ``400`` with
    ``{"error": "invalid_tenant"}`` (AADSTS90002 "Tenant not found"; the empty GUID gets the
    same ``invalid_tenant`` shape as AADSTS900021). Anything else — a 5xx, a 400 *without*
    ``invalid_tenant``, an unparseable body — is INDETERMINATE, never silently treated as
    existence or nonexistence.
    """
    if status_code == 200:
        return TenantExistence.EXISTS
    if status_code == 400 and "invalid_tenant" in body:
        return TenantExistence.NOT_FOUND
    return TenantExistence.INDETERMINATE


async def probe_tenant_existence(tenant_id: str) -> TenantExistence:
    """Ask Entra whether ``tenant_id`` names a real tenant — unauthenticated, no credential.

    The OIDC discovery endpoint is public and stable (every MSAL library resolves tenants
    through it). Any transport failure maps to INDETERMINATE rather than raising: onboarding
    must not become unavailable because login.microsoftonline.com hiccuped, and a tenant record
    grants no authority on its own (consent is still the gate), so an indeterminate probe is
    advisory-only — see ``create_tenant``.
    """
    url = f"https://login.microsoftonline.com/{tenant_id}/v2.0/.well-known/openid-configuration"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
        return evaluate_tenant_existence(response.status_code, response.text)
    except httpx.HTTPError:
        return TenantExistence.INDETERMINATE


# Microsoft Graph's default scope for the v2 admin-consent endpoint — grants everything the app
# registration's own configured permissions declare, rather than naming individual scopes here
# (which would drift from whatever app-roles.json/Graph permissions the app actually has).
_CONSENT_SCOPE = "https://graph.microsoft.com/.default"

# Same shape as CustomerTenant.notification_email's own pattern — one definition of a valid
# address per codebase.
_ADMIN_EMAIL_PATTERN = r"^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$"

# Built-in RBAC role definition GUIDs — fixed, well-known Azure identifiers, not configuration.
CONTRIBUTOR_ROLE_DEFINITION_ID = "b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_ASSIGNMENT_API_VERSION = "2022-04-01"
RESOURCE_GROUP_API_VERSION = "2021-04-01"


class TenantOnboardingNotConfiguredError(HTTPException):
    """The admin-consent redirect URI isn't configured yet — built but not broken, same pattern
    as ``api/voice.py``'s ``VoiceNotConfiguredError``."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=(
                "Tenant onboarding is not configured. Set GROUNDWORK_ENTRA_APP_REDIRECT_URI "
                "(scripts/postprovision.ps1 registers this automatically on provision)."
            ),
        )


class CreateTenantRequest(BaseModel):
    """What a Groundwork operator supplies to register a new customer engagement.

    ``tenantId`` is the customer's own Entra tenant id, known from the sales/onboarding
    conversation — never inferred. There is no ``consentState`` field: every tenant created here
    starts ``PENDING``, and the only way to a granted tenant is the separate confirm route.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    tenant_id: Annotated[str, Field(alias="tenantId", pattern=r"^[0-9a-fA-F-]{36}$")]
    display_name: Annotated[str, Field(alias="displayName", min_length=1)]
    approved_regions: Annotated[frozenset[str], Field(alias="approvedRegions", min_length=1)]
    data_residency_regions: Annotated[
        frozenset[str], Field(alias="dataResidencyRegions", min_length=1)
    ]
    subscriptions: Annotated[tuple[SubscriptionEntitlement, ...], Field(alias="subscriptions")] = ()
    concurrency_cap: Annotated[int, Field(alias="concurrencyCap", ge=1, le=50)] = 3


class ConfirmConsentRequest(BaseModel):
    """The operator's attestation that consent was actually granted (see module docstring for
    why this, not an automated callback, is what authorises this transition)."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    note: Annotated[str, Field(min_length=1, max_length=500)]
    """Free-text record of how the operator verified consent (e.g. "confirmed by customer admin
    Jane Doe via email 2026-08-06", "checked Entra admin center enterprise apps list directly").
    Required, not optional — an attestation with no stated basis is not a real attestation."""


class SetVoiceChannelEnabledRequest(BaseModel):
    """Operator toggle for FR-053e's per-tenant voice enablement.

    Independent of both ``consent_state`` (Lighthouse delegation) and
    ``offshore_inference_consent`` (FR-053d) — see
    :class:`~groundwork_channels.voice.enablement.VoiceEnablementGate`'s three-condition gate.
    This is the piece of that gate a Groundwork operator decides directly (has this engagement
    been sold/scoped to include the voice channel), rather than something a customer or an
    automated callback establishes. Requires a note, the same attestation shape as every other
    operator-driven change to this record — a toggle with no stated reason is not auditable."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    enabled: bool
    note: Annotated[str, Field(min_length=1, max_length=500)]


class ConfirmAdoOrgAccessRequest(BaseModel):
    """FR-038b. Same shape and reasoning as :class:`ConfirmConsentRequest` — a Groundwork
    operator attests that the customer's admin added System's Entra identity as a member of their
    Azure DevOps organisation, verified out-of-band. Azure DevOps organisation membership has no
    admin-consent-style redirect flow to build a self-service verification on top of, so this is
    the real mechanism here too, not a stub standing in for one."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    note: Annotated[str, Field(min_length=1, max_length=500)]


class RecordSubscriptionEntitlementRequest(BaseModel):
    """Operator attestation that a customer subscription is authorised for this tenant (FR-008).

    Before this route existed, nothing anywhere ever constructed a :class:`SubscriptionEntitlement`
    outside a test fixture: ``generate_plan``, ``bootstrap_subscription_identity``, and the
    readiness report all read ``CustomerTenant.subscriptions``/``entitlement_for`` as their source
    of truth, but no operator-facing surface (REST or voice/chat tool) ever wrote to it — the same
    "gated precondition with no real mutation path" shape already found in ``voice_channel_enabled``
    and ``offshore_inference_consent`` before this tenant model grew a demo-only direct-write
    workaround for those two. ``may_deploy`` has no default and is required explicitly: whether a
    subscription may only be planned against (read-only) or actually deployed into is a deliberate
    operator decision, not something that should default either way."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    subscription_id: Annotated[str, Field(alias="subscriptionId", pattern=r"^[0-9a-fA-F-]{36}$")]
    display_name: Annotated[str, Field(alias="displayName", min_length=1)]
    may_deploy: Annotated[bool, Field(alias="mayDeploy")]
    note: Annotated[str, Field(min_length=1, max_length=500)]


def _default_subscriptionless_tenant(
    *, request: Request | WebSocket, caller: AuthenticatedCaller, display_name: str
) -> CreateTenantRequest:
    return CreateTenantRequest(
        tenant_id=caller.tenant_id,
        display_name=display_name,
        approved_regions=frozenset({request.app.state.settings.azure_location}),
        data_residency_regions=frozenset({request.app.state.settings.azure_location}),
        subscriptions=(),
        concurrency_cap=request.app.state.settings.governance.default_tenant_concurrency_cap,
    )


async def create_customer_tenant_record(
    *, body: CreateTenantRequest, request: Request | WebSocket, caller: AuthenticatedCaller
) -> CustomerTenant:
    caller.require_role(CallerRole.OPERATOR)

    tenant = CustomerTenant(
        tenant_id=body.tenant_id,
        display_name=body.display_name,
        consent_state=ConsentState.PENDING,
        approved_regions=body.approved_regions,
        data_residency_regions=body.data_residency_regions,
        subscriptions=body.subscriptions,
        concurrency_cap=body.concurrency_cap,
    )

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    try:
        return await tenant_repository.create(body.tenant_id, tenant)
    except CosmosResourceExistsError as exc:
        raise HTTPException(
            status_code=409, detail="a tenant with this tenantId already exists"
        ) from exc


async def confirm_customer_consent_attestation(
    *,
    tenant_id: str,
    note: str,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> CustomerTenant:
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    if tenant.consent_state is not ConsentState.PENDING:
        raise HTTPException(
            status_code=409,
            detail=(
                f"tenant consent_state is {tenant.consent_state.value!r}, not 'pending'. "
                + (
                    "Consent was already confirmed. "
                    "GET /v1/tenants/{tenantId}/onboarding/status to see the current state."
                    if tenant.consent_state is ConsentState.GRANTED
                    else "Consent was revoked. Contact the customer to re-grant."
                )
            ),
        )

    updated = tenant.model_copy(
        update={
            "consent_state": ConsentState.GRANTED,
            "consent_granted_at": _now(request),
            "consent_confirmed_by_object_id": caller.object_id,
            "consent_confirmed_by_display_name": caller.display_name,
            "consent_confirmation_note": note,
        }
    )
    updated = CustomerTenant.model_validate(updated.model_dump())
    return await tenant_repository.replace(tenant_id, updated)


async def attach_offshore_inference_consent(
    *, tenant_id: str, consent: OffshoreInferenceConsent, request: Request | WebSocket
) -> CustomerTenant | None:
    """Attach a just-recorded offshore-inference consent artefact to the tenant's own record.

    Recording the artefact (``OffshoreInferenceConsentStore.record``, an immutable FR-053d blob)
    and enabling voice for the tenant used to be two disconnected things: nothing ever copied the
    artefact onto ``CustomerTenant.offshore_inference_consent``, the field
    ``VoiceEnablementGate.check`` actually reads, so recording consent never actually enabled
    anything. This closes that gap — called from both consent-recording routes
    (``POST /v1/voice/consent`` and its REST twin here) right after the artefact write succeeds.

    Returns ``None``, not an error, when no tenant record exists yet: the artefact itself is
    still durably recorded either way, and a caller consenting before formal onboarding
    shouldn't have that consent silently discarded — there is just nothing to attach it to until
    :func:`create_customer_tenant_record` runs. The caller is expected to disposition ``None``
    itself; it is not necessarily a defect at the call site.
    """
    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        return None
    updated = tenant.model_copy(update={"offshore_inference_consent": consent})
    updated = CustomerTenant.model_validate(updated.model_dump())
    return await tenant_repository.replace(tenant_id, updated)


async def record_subscription_entitlement(
    *,
    tenant_id: str,
    subscription_id: str,
    display_name: str,
    may_deploy: bool,
    note: str,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> CustomerTenant:
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    if tenant.entitlement_for(subscription_id) is not None:
        raise HTTPException(
            status_code=409,
            detail=f"subscription {subscription_id!r} is already entitled for this tenant",
        )

    entitlement = SubscriptionEntitlement(
        subscription_id=subscription_id,
        display_name=display_name,
        may_deploy=may_deploy,
    )
    updated = tenant.model_copy(update={"subscriptions": (*tenant.subscriptions, entitlement)})
    updated = CustomerTenant.model_validate(updated.model_dump())
    replaced = await tenant_repository.replace(tenant_id, updated)
    logger.info(
        "subscription entitlement recorded tenant=%s subscription=%s may_deploy=%s "
        "operator=%s note=%s",
        tenant_id,
        subscription_id,
        may_deploy,
        caller.object_id,
        note,
    )
    return replaced


async def confirm_customer_ado_org_access_attestation(
    *,
    tenant_id: str,
    note: str,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> CustomerTenant:
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    if tenant.ado_org_access_state is not AdoOrgAccessState.PENDING:
        raise HTTPException(
            status_code=409,
            detail=f"tenant ado_org_access_state is {tenant.ado_org_access_state.value!r}, not "
            "'pending'; this route only transitions a pending grant to granted",
        )

    updated = tenant.model_copy(
        update={
            "ado_org_access_state": AdoOrgAccessState.GRANTED,
            "ado_org_access_granted_at": _now(request),
            "ado_org_access_confirmed_by_object_id": caller.object_id,
            "ado_org_access_confirmed_by_display_name": caller.display_name,
            "ado_org_access_confirmation_note": note,
        }
    )
    updated = CustomerTenant.model_validate(updated.model_dump())
    return await tenant_repository.replace(tenant_id, updated)


def _resolve_devops_organization_url(
    tenant: CustomerTenant, request: Request | WebSocket
) -> str | None:
    return (
        tenant.devops_organization_url
        or request.app.state.settings.readiness.devops_organization_url
    )


def _ado_membership_guidance(status_code: int, organization_url: str | None) -> str:
    if status_code in (401, 403):
        return (
            "Groundwork is an Azure DevOps organization member, but Project Collection "
            "Administrators access is still missing."
        )
    if status_code == 200:
        return "Groundwork can reach the Azure DevOps organization as a member."
    if organization_url is None:
        return "No Azure DevOps organization URL is recorded for this tenant."
    return f"Azure DevOps organization access could not be verified for {organization_url}."


async def grant_customer_ado_org_access(
    *, tenant_id: str, request: Request | WebSocket, caller: AuthenticatedCaller
) -> dict[str, object]:
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    organization_url = _resolve_devops_organization_url(tenant, request)
    if organization_url is None:
        raise HTTPException(
            status_code=409,
            detail=(
                "cannot grant Azure DevOps organization access: neither the tenant record nor "
                "the control-plane settings provide a devops_organization_url"
            ),
        )

    instructions = build_azure_devops_instructions(
        tenant=tenant,
        settings=request.app.state.settings,
        organization_url=organization_url,
    )
    token = await request.app.state.credential.get_token(f"{AZURE_DEVOPS_RESOURCE_ID}/.default")
    token_value = getattr(token, "token", None)
    if not isinstance(token_value, str):
        raise HTTPException(
            status_code=502,
            detail="Azure DevOps credential did not return a bearer token string",
        )

    injected_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    owns_client = injected_client is None
    http_client = injected_client or httpx.AsyncClient(timeout=30.0)
    try:
        # This call authenticates as the control plane's own identity. A brand-new Azure DevOps
        # organization has never heard of that identity either, and the entitlement call below
        # 401s with a misleading "sign in at least once" error regardless of the *target*
        # (orchestrator) identity's own state — found live 2026-09-07. Self-entitling first is
        # idempotent (200/201/409 if already known) and turns that dead end into a clear,
        # complete instruction set instead of an infinite retry loop.
        if instructions.control_plane_entitlement_request_body is not None:
            control_plane_response = await http_client.post(
                instructions.entitlement_endpoint,
                headers={
                    "Authorization": f"Bearer {token_value}",
                    "Content-Type": "application/json",
                },
                json=dict(instructions.control_plane_entitlement_request_body),
            )
            if control_plane_response.status_code not in (200, 201, 409):
                return {
                    "status": "error",
                    "verified": False,
                    "reason": "control_plane_identity_not_recognised",
                    "next_action": instructions.control_plane_entitlement_instruction_text,
                    "evidence": {
                        "organizationUrl": organization_url,
                        "entitlementEndpoint": instructions.entitlement_endpoint,
                        "controlPlanePrincipalObjectId": (
                            instructions.control_plane_principal_object_id
                        ),
                        "controlPlaneEntitlementStatusCode": control_plane_response.status_code,
                        "message": scrub_text(control_plane_response.text),
                    },
                }

        response = await http_client.post(
            instructions.entitlement_endpoint,
            headers={
                "Authorization": f"Bearer {token_value}",
                "Content-Type": "application/json",
            },
            json=dict(instructions.entitlement_request_body),
        )
        if response.status_code not in (200, 201, 409):
            return {
                "status": "error",
                "verified": False,
                "reason": "ado_entitlement_request_failed",
                "next_action": (
                    "Have a customer Azure DevOps organization owner run the entitlement call "
                    "manually and then retry this step."
                ),
                "evidence": {
                    "organizationUrl": organization_url,
                    "entitlementEndpoint": instructions.entitlement_endpoint,
                    "entitlementStatusCode": response.status_code,
                    "message": scrub_text(response.text),
                },
            }

        probe_status = await probe_organization_status(
            organization_url,
            request.app.state.credential,
            http_client=http_client,
        )
    finally:
        if owns_client:
            await http_client.aclose()

    evidence = {
        "organizationUrl": organization_url,
        "entitlementEndpoint": instructions.entitlement_endpoint,
        "entitlementStatusCode": response.status_code,
        "probeStatusCode": probe_status,
    }
    if probe_status == 200:
        return {
            "status": "member",
            "verified": True,
            "next_action": "Continue onboarding; Azure DevOps organization membership is verified.",
            "evidence": evidence,
        }
    if probe_status in (401, 403):
        return {
            "status": "member_pending_pca",
            "verified": False,
            "next_action": instructions.pca_instruction_text,
            "evidence": evidence,
            "pcaInstructionText": instructions.pca_instruction_text,
        }
    return {
        "status": "error",
        "verified": False,
        "reason": "ado_membership_unverified",
        "next_action": _ado_membership_guidance(probe_status, organization_url),
        "evidence": evidence,
    }


async def bootstrap_subscription_identity(
    *,
    tenant_id: str,
    subscription_id: str,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> CustomerTenant:
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    if not tenant.consent_state.permits_tenant_operations:
        raise HTTPException(
            status_code=409,
            detail=f"tenant consent_state is {tenant.consent_state.value!r}, not 'granted'; "
            "Lighthouse delegation must be established before bootstrap can write into the "
            "subscription (FR-006a)",
        )
    entitlement = tenant.entitlement_for(subscription_id)
    if entitlement is None:
        raise HTTPException(
            status_code=404,
            detail=f"subscription {subscription_id!r} is not entitled for this tenant",
        )
    if entitlement.bootstrap_identity_resource_id is not None:
        return tenant
    if not tenant.devops_organization_url:
        raise BootstrapIdentityNotConfiguredError()

    credential = request.app.state.credential
    resource_group = deployment_resource_group_name(subscription_id)
    identity_name = managed_identity_name(subscription_id)
    location = next(iter(tenant.approved_regions))

    arm_token = await credential.get_token(ARM_SCOPE)
    arm_headers = {
        "Authorization": f"Bearer {arm_token.token}",
        "Content-Type": "application/json",
    }
    identity_resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group}/providers/"
        f"Microsoft.ManagedIdentity/userAssignedIdentities/{identity_name}"
    )
    identity_url = (
        f"{ARM_ENDPOINT}{identity_resource_id}?api-version={FEDERATED_CREDENTIAL_API_VERSION}"
    )

    injected_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    owns_client = injected_client is None
    http_client = injected_client or httpx.AsyncClient(timeout=30.0)
    try:
        rg_url = (
            f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/resourceGroups/{resource_group}"
            f"?api-version={RESOURCE_GROUP_API_VERSION}"
        )
        rg_response = await http_client.put(
            rg_url, headers=arm_headers, json={"location": location}
        )
        rg_response.raise_for_status()

        identity_response = await http_client.put(
            identity_url, headers=arm_headers, json={"location": location}
        )
        identity_response.raise_for_status()
        identity_body = identity_response.json()
        client_id = str(identity_body["properties"]["clientId"])

        organization_id = await _get_ado_organization_id(
            credential, tenant.devops_organization_url, http_client
        )
        issuer = f"https://vstoken.dev.azure.com/{organization_id}"
        subject = _expected_subject(tenant.devops_organization_url, subscription_id)
        fic_resource_id = federated_credential_resource_id(subscription_id)
        fic_url = f"{ARM_ENDPOINT}{fic_resource_id}?api-version={FEDERATED_CREDENTIAL_API_VERSION}"
        fic_response = await http_client.put(
            fic_url,
            headers=arm_headers,
            json={
                "properties": {
                    "issuer": issuer,
                    "subject": subject,
                    "audiences": [TOKEN_EXCHANGE_AUDIENCE],
                }
            },
        )
        fic_response.raise_for_status()

        principal_id = str(identity_body["properties"]["principalId"])
        role_assignment_name = str(uuid.uuid5(uuid.NAMESPACE_URL, identity_resource_id))
        role_assignment_url = (
            f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/providers/"
            f"Microsoft.Authorization/roleAssignments/{role_assignment_name}"
            f"?api-version={ROLE_ASSIGNMENT_API_VERSION}"
        )
        role_response = await http_client.put(
            role_assignment_url,
            headers=arm_headers,
            json={
                "properties": {
                    "roleDefinitionId": (
                        f"/subscriptions/{subscription_id}/providers/Microsoft.Authorization/"
                        f"roleDefinitions/{CONTRIBUTOR_ROLE_DEFINITION_ID}"
                    ),
                    "principalId": principal_id,
                    "principalType": "ServicePrincipal",
                }
            },
        )
        if role_response.status_code not in (200, 201, 409):
            role_response.raise_for_status()
    finally:
        if owns_client:
            await http_client.aclose()

    now = _now(request)
    updated_entitlement = entitlement.model_copy(
        update={
            "bootstrap_identity_resource_id": identity_resource_id,
            "bootstrap_identity_client_id": client_id,
            "bootstrap_identity_created_at": now,
        }
    )
    updated_subscriptions = tuple(
        updated_entitlement if s.subscription_id == entitlement.subscription_id else s
        for s in tenant.subscriptions
    )
    updated_tenant = tenant.model_copy(update={"subscriptions": updated_subscriptions})
    updated_tenant = CustomerTenant.model_validate(updated_tenant.model_dump())

    replaced = await tenant_repository.replace(tenant_id, updated_tenant)
    logger.info(
        "bootstrap identity created",
        extra={
            "event": "bootstrap_identity_created",
            "tenant_id": tenant_id,
            "subscription_id": subscription_id,
            "identity_resource_id": identity_resource_id,
            "federated_credential_name": federated_credential_name(subscription_id),
        },
    )
    return replaced


def _tenant_response(tenant: CustomerTenant) -> dict[str, object]:
    return {
        "tenantId": tenant.tenant_id,
        "displayName": tenant.display_name,
        "consentState": tenant.consent_state.value,
        "consentGrantedAt": (
            tenant.consent_granted_at.isoformat() if tenant.consent_granted_at else None
        ),
        "consentConfirmedBy": (
            {
                "objectId": tenant.consent_confirmed_by_object_id,
                "displayName": tenant.consent_confirmed_by_display_name,
                "note": tenant.consent_confirmation_note,
            }
            if tenant.consent_confirmed_by_object_id
            else None
        ),
        "adoOrgAccessState": tenant.ado_org_access_state.value,
        "adoOrgAccessGrantedAt": (
            tenant.ado_org_access_granted_at.isoformat()
            if tenant.ado_org_access_granted_at
            else None
        ),
        "adoOrgAccessConfirmedBy": (
            {
                "objectId": tenant.ado_org_access_confirmed_by_object_id,
                "displayName": tenant.ado_org_access_confirmed_by_display_name,
                "note": tenant.ado_org_access_confirmation_note,
            }
            if tenant.ado_org_access_confirmed_by_object_id
            else None
        ),
        "approvedRegions": sorted(tenant.approved_regions),
        "dataResidencyRegions": sorted(tenant.data_residency_regions),
        "subscriptions": [
            {
                "subscriptionId": s.subscription_id,
                "displayName": s.display_name,
                "mayDeploy": s.may_deploy,
                "bootstrapIdentityResourceId": s.bootstrap_identity_resource_id,
                "bootstrapIdentityClientId": s.bootstrap_identity_client_id,
                "bootstrapIdentityCreatedAt": (
                    s.bootstrap_identity_created_at.isoformat()
                    if s.bootstrap_identity_created_at
                    else None
                ),
            }
            for s in tenant.subscriptions
        ],
        "concurrencyCap": tenant.concurrency_cap,
        "voiceChannelEnabled": tenant.voice_channel_enabled,
        "offshoreInferenceConsent": (
            {
                "consentingIdentityObjectId": (
                    tenant.offshore_inference_consent.consenting_identity_object_id
                ),
                "consentingIdentityDisplayName": (
                    tenant.offshore_inference_consent.consenting_identity_display_name
                ),
                "consentedAt": tenant.offshore_inference_consent.consented_at.isoformat(),
                "disclosureVersion": tenant.offshore_inference_consent.disclosure_version,
            }
            if tenant.offshore_inference_consent
            else None
        ),
        "notificationEmail": tenant.notification_email,
        "contactDisplayName": tenant.contact_display_name,
    }


def _now(request: Request | WebSocket) -> datetime:
    now_fn = getattr(request.app.state, "now_fn", None)
    return now_fn() if now_fn is not None else datetime.now(UTC)


@router.post("", status_code=201)
async def create_tenant(
    body: CreateTenantRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Register a new customer engagement, ``consent_state`` always starting ``PENDING``."""
    created = await create_customer_tenant_record(body=body, request=request, caller=caller)
    return _tenant_response(created)


@router.get("/{tenant_id}")
async def get_tenant(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return _tenant_response(tenant)


@router.get("/{tenant_id}/onboarding/consent-url")
async def get_consent_url(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """The real Microsoft admin-consent URL for this tenant - send it to the customer's admin.

    Pure computation, no write: this route never records anything, because the URL alone proves
    nothing was granted yet (see module docstring - the redirect back from Entra is not trusted
    either). ``POST .../confirm`` is the only route that changes ``consent_state``.
    """
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    return {"tenantId": tenant_id, "consentUrl": _consent_url(request, tenant_id)}


def _consent_url(request: Request, tenant_id: str) -> str:
    """Build the real Microsoft v2 admin-consent URL for one tenant (shared by the GET route
    and the email-invite route below)."""
    entra = request.app.state.settings.entra
    if not entra.redirect_uri:
        raise TenantOnboardingNotConfiguredError()

    query = urlencode(
        {
            "client_id": entra.client_id,
            "scope": _CONSENT_SCOPE,
            "redirect_uri": entra.redirect_uri,
            "state": tenant_id,
        }
    )
    return f"https://login.microsoftonline.com/{tenant_id}/v2.0/adminconsent?{query}"


@router.post("/{tenant_id}/onboarding/confirm")
async def confirm_consent(
    tenant_id: str,
    body: ConfirmConsentRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Operator attestation that consent was actually granted — see module docstring for why
    this, not an automated callback, is the real verification mechanism today."""
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tenant {tenant_id!r} not found. "
                "POST /v1/tenants to create the tenant record first."
            ),
        )
    replaced = await confirm_customer_consent_attestation(
        tenant_id=tenant_id,
        note=body.note,
        request=request,
        caller=caller,
    )
    return _tenant_response(replaced)


@router.post("/{tenant_id}/voice-channel")
async def set_voice_channel_enabled(
    tenant_id: str,
    body: SetVoiceChannelEnabledRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Operator enable/disable of the voice channel for one tenant — see
    :class:`SetVoiceChannelEnabledRequest` for why this is a separate toggle from consent."""
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tenant {tenant_id!r} not found. "
                "POST /v1/tenants to create the tenant record first."
            ),
        )
    updated = tenant.model_copy(update={"voice_channel_enabled": body.enabled})
    updated = CustomerTenant.model_validate(updated.model_dump())
    replaced = await tenant_repository.replace(tenant_id, updated)
    return _tenant_response(replaced)


@router.post("/{tenant_id}/subscriptions", status_code=201)
async def add_subscription_entitlement(
    tenant_id: str,
    body: RecordSubscriptionEntitlementRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Operator attestation that authorises one subscription for this tenant (FR-008) — see
    :class:`RecordSubscriptionEntitlementRequest` for why this route exists at all."""
    replaced = await record_subscription_entitlement(
        tenant_id=tenant_id,
        subscription_id=body.subscription_id,
        display_name=body.display_name,
        may_deploy=body.may_deploy,
        note=body.note,
        request=request,
        caller=caller,
    )
    return _tenant_response(replaced)


@router.post("/{tenant_id}/onboarding/ado-access-confirm")
async def confirm_ado_org_access(
    tenant_id: str,
    body: ConfirmAdoOrgAccessRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """FR-038b. Operator attestation that the customer's admin added System's Entra identity to
    their Azure DevOps organisation — independent of `consent_state` (Lighthouse delegation has no
    jurisdiction over Azure DevOps organisation membership). Gates `devops_project` execution
    specifically (`engine/sequencer.py`'s `ado_org_access_check`), not the rest of a deployment."""
    replaced = await confirm_customer_ado_org_access_attestation(
        tenant_id=tenant_id,
        note=body.note,
        request=request,
        caller=caller,
    )
    return _tenant_response(replaced)


@router.post("/{tenant_id}/onboarding/ado-access-grant")
async def grant_ado_org_access(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Run the Azure DevOps servicePrincipalEntitlements grant and verify the outcome."""
    return await grant_customer_ado_org_access(
        tenant_id=tenant_id,
        request=request,
        caller=caller,
    )


# ---------------------------------------------------------------------------
# Bootstrap identity (FR-006a) — the one direct-ARM write System ever makes into a customer
# subscription. Per-subscription, onboarding-time, idempotent (analyzer finding I1 and its
# 2026-08-24 follow-up — see spec.md's FR-006a for the full reasoning).
# ---------------------------------------------------------------------------


class BootstrapIdentityNotConfiguredError(HTTPException):
    """Bootstrap needs the tenant's Azure DevOps organisation URL to compute the federated
    credential's subject claim — same "declared, not guessed" discipline as
    ``devops_organization_url``'s every other consumer."""

    def __init__(self) -> None:
        super().__init__(
            status_code=409,
            detail=(
                "cannot bootstrap: this tenant has no devops_organization_url recorded yet — "
                "gather it before attempting bootstrap"
            ),
        )


async def _get_ado_organization_id(
    credential: TokenCredentialLike, organization_url: str, http_client: httpx.AsyncClient
) -> str:
    """Duplicated from ``stages/identity.py``'s own ``_get_organization_id`` rather than
    imported — that method is bound to ``IdentityStage``'s own ``httpx.AsyncClient`` instance, and
    ``identity.py``'s real Azure work is moving to trigger-and-poll (FR-038a, T080), which will
    make this exact method dead code there. Must stay byte-identical to what it duplicates:
    Azure DevOps' `connectionData` API is the only way to resolve an organisation's GUID, which
    the federated credential's `issuer` claim requires."""
    token = await credential.get_token(f"{AZURE_DEVOPS_RESOURCE_ID}/.default")
    token_value = getattr(token, "token", None)
    if not isinstance(token_value, str):
        raise HTTPException(
            status_code=502,
            detail="Azure DevOps credential did not return a bearer token string",
        )
    url = f"{organization_url}/_apis/connectionData?api-version={CONNECTION_DATA_API_VERSION}"
    response = await http_client.get(url, headers={"Authorization": f"Bearer {token_value}"})
    response.raise_for_status()
    instance_id = response.json().get("instanceId")
    if not instance_id:
        raise HTTPException(
            status_code=502,
            detail=f"Azure DevOps connectionData for {organization_url!r} did not return an "
            f"instanceId; cannot resolve the organisation GUID the federated credential's "
            f"issuer requires",
        )
    return str(instance_id)


@router.post("/{tenant_id}/subscriptions/{subscription_id}/bootstrap-identity", status_code=201)
async def bootstrap_identity(
    tenant_id: str,
    subscription_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """FR-006a. Create this subscription's bootstrap user-assigned managed identity and its
    federated credential, using System's Lighthouse-delegated access (FR-006) — the only
    direct-ARM write System ever makes into a customer subscription.

    Idempotent, check-before-write: a subscription that already has a recorded
    ``bootstrapIdentityResourceId`` returns that existing state (``200``) rather than attempting
    to recreate it — matching every other onboarding route's discipline. Requires
    ``consent_state`` already ``GRANTED`` (Lighthouse delegated) and ``devops_organization_url``
    already recorded, since the federated credential's ``subject`` claim needs the specific Azure
    DevOps organisation and (deterministically-named) service connection it will trust.

    **[VERIFIED]** against Microsoft Learn (retrieved 2026-08-24, see the research notes):
    ``Microsoft.ManagedIdentity/userAssignedIdentities`` create-or-update
    (api-version ``2024-11-30``, ``{"location": ..., "tags": ...}`` body,
    ``properties.clientId``/``properties.principalId`` in the response) and
    ``.../federatedIdentityCredentials`` create-or-update (same api-version,
    ``{"properties": {"issuer": ..., "subject": ..., "audiences": [...]}}``). The issuer/subject
    computation (``https://vstoken.dev.azure.com/{organizationId}``,
    ``sc://{org}/{project}/{connectionName}``) reuses ``stages/identity.py``'s own already-verified
    pattern rather than re-deriving it.
    """
    replaced = await bootstrap_subscription_identity(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        request=request,
        caller=caller,
    )
    return _tenant_response(replaced)


# ---------------------------------------------------------------------------
# Email invite — the operator sends the admin-consent URL instead of copy/pasting it
# ---------------------------------------------------------------------------


class InviteAdminRequest(BaseModel):
    """``{ adminEmail, adminDisplayName? }`` — who receives the onboarding invite."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    admin_email: Annotated[str, Field(alias="adminEmail", pattern=_ADMIN_EMAIL_PATTERN)]
    admin_display_name: Annotated[
        str | None, Field(alias="adminDisplayName", min_length=1, max_length=200)
    ] = None


class TenantEmailNotConfiguredError(HTTPException):
    """Email sending is built but no ACS Email endpoint/sender is configured."""

    def __init__(self) -> None:
        super().__init__(
            status_code=503,
            detail=(
                "Email sending is built but not configured. Set GROUNDWORK_ACS_EMAIL_ENDPOINT "
                "and GROUNDWORK_ACS_EMAIL_SENDER_ADDRESS."
            ),
        )


class RecordOffshoreInferenceConsentRequest(BaseModel):
    """REST twin for ``POST /v1/voice/consent`` with the tenant bound to the caller token."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    disclosure_version: Annotated[
        str, Field(alias="disclosureVersion", pattern=r"^\d+\.\d+\.\d+$")
    ] = CURRENT_DISCLOSURE_VERSION


@router.get("/onboarding/offshore-inference-disclosure")
async def get_offshore_inference_disclosure() -> dict[str, str]:
    """Serve the current FR-053d offshore-inference disclosure text verbatim.

    The REST twin of the voice ``get_offshore_inference_disclosure`` tool: consent is only
    meaningful against the disclosure the customer was actually shown
    (``groundwork_contracts/tenant.py``'s own warning), so the text has to be fetchable by every
    surface that records consent, not just the voice channel. Requires no role — the disclosure
    is public product content, and gating it behind the operator role would stop the customer's
    own admin from reading it before consenting.
    """
    return {
        "disclosureVersion": CURRENT_DISCLOSURE_VERSION,
        "disclosure": OFFSHORE_INFERENCE_DISCLOSURE,
    }


@router.post("/offshore-inference-consent", status_code=201)
async def record_offshore_inference_consent(
    body: RecordOffshoreInferenceConsentRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Record offshore-inference consent for the authenticated caller's own tenant (FR-007).

    Also attaches the recorded consent to the tenant's own record via
    :func:`attach_offshore_inference_consent` — see that function's docstring for why.
    """
    if body.disclosure_version != CURRENT_DISCLOSURE_VERSION:
        raise HTTPException(
            status_code=409,
            detail=(
                "disclosureVersion does not match the currently published offshore-inference "
                f"disclosure version {CURRENT_DISCLOSURE_VERSION!r}"
            ),
        )
    consent_store: OffshoreInferenceConsentStore = request.app.state.consent_store
    consent = await consent_store.record(
        consent_id=str(uuid.uuid4()),
        consenting_identity_object_id=caller.object_id,
        consenting_identity_display_name=caller.display_name,
        disclosure_version=body.disclosure_version,
        now=_now(request),
    )
    await attach_offshore_inference_consent(
        tenant_id=caller.tenant_id, consent=consent, request=request
    )
    return consent.model_dump(mode="json")


@router.post("/{tenant_id}/invite", status_code=201)
async def invite_admin(
    tenant_id: str,
    body: InviteAdminRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Email the tenant's admin the real admin-consent URL (the operator invite step).

    Operator-gated like every other onboarding route. The email carries exactly what
    ``GET .../onboarding/consent-url`` returns - Microsoft's own consent flow does the
    authorizing; this route only delivers the link and records that it was sent. Consent
    itself is still confirmed separately via ``POST .../onboarding/confirm``.
    """
    caller.require_role(CallerRole.OPERATOR)

    settings = request.app.state.settings
    if not settings.acs_email_endpoint or not settings.acs_email_sender_address:
        raise TenantEmailNotConfiguredError()

    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")

    consent_url = _consent_url(request, tenant_id)
    display_name = body.admin_display_name or body.admin_email
    subject = f"Groundwork: approve platform access for {tenant.display_name}"
    plain_text = (
        f"Hello,\n\n"
        f"Groundwork has been requested to provision and manage data platforms in your "
        f"Azure tenant ({tenant_id}). To allow this, a Global Administrator or Application "
        f"Administrator must approve the platform's access:\n\n"
        f"{consent_url}\n\n"
        f"Approving grants the Groundwork application the permissions it needs to deploy "
        f"into your subscription after you request a deployment. If you were not expecting "
        f"this request, ignore this email and contact your Groundwork operator.\n\n"
        f"— Groundwork Platform"
    )
    html = (
        '<html lang="en-AU"><head><meta charset="utf-8">'
        f"<title>Groundwork access invitation</title></head><body>"
        f"<h1>Approve Groundwork platform access</h1>"
        f"<p>Groundwork has been requested to provision and manage data platforms in your "
        f"Azure tenant (<code>{tenant_id}</code>).</p>"
        f'<p><a href="{consent_url}">Review and approve access</a> '
        f"(requires a Global Administrator or Application Administrator).</p>"
        f"<p>Approving grants the Groundwork application the permissions it needs to deploy "
        f"into your subscription after you request a deployment. If you were not expecting "
        f"this request, ignore this email and contact your Groundwork operator.</p>"
        f"</body></html>"
    )

    sender = build_email_sender(
        acs_endpoint=settings.acs_email_endpoint,
        sender_address=settings.acs_email_sender_address,
        credential=request.app.state.credential,
    )
    try:
        operation_id = await sender.send(
            to_address=body.admin_email,
            to_display_name=display_name,
            subject=subject,
            plain_text=plain_text,
            html=html,
        )
    except NotificationDispatchError as exc:
        raise HTTPException(status_code=502, detail=f"email send failed: {exc}") from exc

    logger.info(
        "tenant invite sent",
        extra={
            "event": "tenant_invite_sent",
            "tenant_id": tenant_id,
            "invited_by": caller.object_id,
            "email_operation_id": operation_id,
        },
    )
    return {
        "inviteId": str(uuid.uuid4()),
        "tenantId": tenant_id,
        "adminEmail": body.admin_email,
        "consentUrl": consent_url,
        "emailOperationId": operation_id,
        "invitedBy": caller.object_id,
    }


# ---------------------------------------------------------------------------
# Notification email — the operator-recorded recipient whose absence used to fail
# silently at notification time instead of loudly at approval time
# ---------------------------------------------------------------------------


class SetNotificationEmailRequest(BaseModel):
    """Operator attestation that records the customer's reconfirmed notification email.

    Same shape and reasoning as :class:`RecordSubscriptionEntitlementRequest`: this is the
    mutation path for a gated precondition that previously had none outside the voice
    conversational flow. ``CustomerTenant.notification_email`` was only ever written by the
    voice plan path's ``persist_engagement_details``; a REST/CLI-only operator had no way to
    record it, so the orchestrator's notifier would silently skip every outcome notification.
    ``note`` is required — recording an email is an audit-trail change, not a form field.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    email: Annotated[str, Field(alias="email", pattern=_ADMIN_EMAIL_PATTERN)]
    display_name: Annotated[
        str | None, Field(alias="displayName", min_length=1, max_length=200)
    ] = None
    note: Annotated[str, Field(min_length=1, max_length=500)]


async def record_tenant_notification_email(
    *,
    tenant_id: str,
    email: str,
    display_name: str | None,
    note: str,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> CustomerTenant:
    """Record (or replace) the tenant's notification recipient — operator-attested.

    Idempotent replace: re-recording the same email with a new note is a legitimate
    re-confirmation, not a conflict. The customer is expected to re-confirm the address during
    conversation (FR-002 / FR-004b); this route is the operator-side equivalent for REST/CLI
    onboarding when no conversational capture has happened.
    """
    caller.require_role(CallerRole.OPERATOR)

    tenant_repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tenant {tenant_id!r} not found. "
                "POST /v1/tenants to create the tenant record first."
            ),
        )

    update: dict[str, str | None] = {
        "notification_email": email,
        "contact_display_name": display_name,
    }
    updated = tenant.model_copy(update=update)
    updated = CustomerTenant.model_validate(updated.model_dump())
    replaced = await tenant_repository.replace(tenant_id, updated)
    logger.info(
        "notification email recorded tenant=%s operator=%s note=%s",
        tenant_id,
        caller.object_id,
        note,
    )
    return replaced


@router.post("/{tenant_id}/notification-email", status_code=201)
async def set_notification_email(
    tenant_id: str,
    body: SetNotificationEmailRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Record the customer's reconfirmed notification email on the tenant record.

    This is the mutation path that makes the approval-time notification gate satisfiable from
    REST/CLI: without it, ``CustomerTenant.notification_email`` could only ever be written by
    the voice conversational flow, and an approval of a plan whose email was never persisted
    would be refused by :class:`~groundwork_controlplane.approval.service.record_approval`
    with ``NotificationEmailMissingError`` (409).
    """
    replaced = await record_tenant_notification_email(
        tenant_id=tenant_id,
        email=body.email,
        display_name=body.display_name,
        note=body.note,
        request=request,
        caller=caller,
    )
    return _tenant_response(replaced)


# ---------------------------------------------------------------------------
# Onboarding status — single read that tells the operator what's done and
# what to do next, replacing the need to manually inspect each field.
# ---------------------------------------------------------------------------


class OnboardingStepStatus(BaseModel):
    """One step's completion state."""

    model_config = ConfigDict(frozen=True)

    completed: bool
    completed_at: str | None = None
    detail: str = ""


class OnboardingStatusResponse(BaseModel):
    """The full onboarding picture for one tenant — what's done, what's next."""

    model_config = ConfigDict(frozen=True, populate_by_name=True)

    tenant_id: Annotated[str, Field(alias="tenantId")]
    consent_state: Annotated[str, Field(alias="consentState")]
    steps: dict[str, OnboardingStepStatus]
    next_action: Annotated[str, Field(alias="nextAction")]


def _build_onboarding_status(tenant: CustomerTenant) -> OnboardingStatusResponse:
    """Derive the onboarding status from the tenant record's current state."""
    steps: dict[str, OnboardingStepStatus] = {}

    # Step 1: Tenant created (always true if we're reading it)
    steps["tenantCreated"] = OnboardingStepStatus(
        completed=True,
        detail="Tenant record exists.",
    )

    # Step 2: Consent confirmed
    if tenant.consent_state is ConsentState.GRANTED:
        steps["consentConfirmed"] = OnboardingStepStatus(
            completed=True,
            completed_at=(
                tenant.consent_granted_at.isoformat() if tenant.consent_granted_at else None
            ),
            detail=(
                f"Consent confirmed by {tenant.consent_confirmed_by_display_name or 'unknown'}."
            ),
        )
    elif tenant.consent_state is ConsentState.REVOKED:
        steps["consentConfirmed"] = OnboardingStepStatus(
            completed=False,
            detail="Consent has been revoked. Contact the customer to re-grant.",
        )
    else:
        steps["consentConfirmed"] = OnboardingStepStatus(
            completed=False,
            detail=(
                "Consent not yet confirmed. Send the admin-consent URL to the customer's "
                "administrator, then confirm via POST /v1/tenants/{tenantId}/onboarding/confirm."
            ),
        )

    # Step 3: Offshore inference consent
    if tenant.offshore_inference_consent is not None:
        steps["offshoreConsentRecorded"] = OnboardingStepStatus(
            completed=True,
            completed_at=tenant.offshore_inference_consent.consented_at.isoformat(),
            detail="Offshore-inference consent recorded.",
        )
    else:
        steps["offshoreConsentRecorded"] = OnboardingStepStatus(
            completed=False,
            detail=(
                "Offshore-inference consent not recorded. "
                "POST /v1/tenants/offshore-inference-consent to record it."
            ),
        )

    # Step 4: Voice channel enabled
    if tenant.voice_channel_enabled:
        steps["voiceEnabled"] = OnboardingStepStatus(
            completed=True,
            detail="Voice channel enabled.",
        )
    else:
        steps["voiceEnabled"] = OnboardingStepStatus(
            completed=False,
            detail=(
                "Voice channel not enabled. "
                "POST /v1/tenants/{tenantId}/voice-channel to enable it."
            ),
        )

    # Determine next action
    next_action = ""
    if tenant.consent_state is ConsentState.PENDING:
        next_action = (
            "Confirm consent: POST /v1/tenants/{tenantId}/onboarding/confirm"
        )
    elif tenant.offshore_inference_consent is None:
        next_action = (
            "Record offshore-inference consent: POST /v1/tenants/offshore-inference-consent"
        )
    elif not tenant.voice_channel_enabled:
        next_action = "Enable voice: POST /v1/tenants/{tenantId}/voice-channel"
    else:
        next_action = "Onboarding complete. Tenant is ready for voice and chat."

    return OnboardingStatusResponse(
        tenant_id=tenant.tenant_id,
        consent_state=tenant.consent_state.value,
        steps=steps,
        next_action=next_action,
    )


@router.get("/{tenant_id}/onboarding/status")
async def get_onboarding_status(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Return the onboarding status for one tenant — what's done and what's next.

    Replaces the need to manually inspect consent_state, offshore_inference_consent,
    and voice_channel_enabled. The ``nextAction`` field tells the operator exactly
    which endpoint to call next.
    """
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    status = _build_onboarding_status(tenant)
    return status.model_dump(by_alias=True)


@router.post("/{tenant_id}/onboarding/verify-consent")
async def verify_consent(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Run the read-only Lighthouse probe and report whether the customer's admin actually
    completed the admin-consent flow.

    This is the evidence-backed alternative to a blind consent attestation. The control plane's
    existing read-only ARM credential checks the customer subscription's
    ``Microsoft.ManagedServices`` registrationAssignments for Groundwork's own principal — no new
    secret and no new authority, it just makes the operator's ``confirm`` step informed instead
    of a guess.

    If delegation is GRANTED and the tenant record is still PENDING (``consentCanBeConfirmed:
    true``), the customer's admin has completed the flow and the operator can confirm with
    confidence. If delegation is PENDING, the admin has not completed it yet (or Groundwork's
    managed-by tenant is not delegated on this subscription) — re-run this check after the admin
    approves. Requires a recorded, deployable subscription entitlement on the tenant (the same
    single-subscription precondition as the onboarding-facts probe it reuses).
    """
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"tenant {tenant_id!r} not found. "
                "POST /v1/tenants to create the tenant record first."
            ),
        )

    facts = await build_tenant_onboarding_facts(
        tenant=tenant,
        settings=request.app.state.settings,
        credential=request.app.state.credential,
        http_client=getattr(request.app.state, "lighthouse_http_client", None),
    )

    delegation = facts.delegation_state
    record_is_granted = tenant.consent_state is ConsentState.GRANTED
    verified = delegation is DelegationState.GRANTED

    if delegation is DelegationState.GRANTED and not record_is_granted:
        next_action = (
            "Delegation is live on the subscription. Confirm consent now with "
            "POST /v1/tenants/{tenantId}/onboarding/confirm."
        )
    elif delegation is DelegationState.GRANTED and record_is_granted:
        next_action = "Consent already confirmed and delegation verified. Nothing to do."
    else:
        next_action = (
            "Lighthouse delegation not found yet. Ask the customer's Global Administrator to "
            "complete the admin-consent flow, then re-run verify-consent."
        )

    return {
        "tenantId": tenant_id,
        "subscriptionId": facts.subscription_id,
        "delegationState": delegation.value,
        "consentState": tenant.consent_state.value,
        "verified": verified,
        "consentCanBeConfirmed": verified and not record_is_granted,
        "lighthouseCommand": facts.lighthouse.az_deployment_command,
        "nextAction": next_action,
    }


# ---------------------------------------------------------------------------
# Quick-onboard — single call that creates a tenant, confirms consent,
# records offshore-inference consent, and optionally enables voice. For
# operators onboarding their own dev tenant where the 4-call sequence is
# unnecessary friction.
# ---------------------------------------------------------------------------


class QuickOnboardRequest(BaseModel):
    """Single-call onboarding for an operator's own dev tenant.

    Combines create_tenant, confirm_consent, offshore_inference_consent, and
    voice_channel_enable into one call. The tenant's consent_state starts PENDING
    and is immediately confirmed to GRANTED within the same call — same audit
    trail as the multi-call sequence (consent_confirmed_by_* fields are set),
    just without the round-trips.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    tenant_id: Annotated[str, Field(alias="tenantId", pattern=r"^[0-9a-fA-F-]{36}$")]
    display_name: Annotated[str, Field(alias="displayName", min_length=1)]
    approved_regions: Annotated[frozenset[str], Field(alias="approvedRegions", min_length=1)]
    data_residency_regions: Annotated[
        frozenset[str], Field(alias="dataResidencyRegions", min_length=1)
    ]
    consent_note: Annotated[str, Field(alias="consentNote", min_length=1, max_length=500)]
    voice_enabled: Annotated[bool, Field(alias="voiceEnabled")] = False
    voice_note: Annotated[
        str | None, Field(alias="voiceNote", min_length=1, max_length=500)
    ] = None


@dataclass(frozen=True, slots=True)
class QuickOnboardParams:
    """Raw quick-onboard parameters — shared by the REST route and the voice tool.

    The voice tool supplies these from model-extracted arguments; the REST route
    supplies them from the Pydantic body. Keeping the two callers on the same
    dataclass means the actual onboarding logic has exactly one implementation.
    """

    tenant_id: str
    display_name: str
    approved_regions: frozenset[str]
    data_residency_regions: frozenset[str]
    consent_note: str
    voice_enabled: bool = False
    voice_note: str | None = None


async def quick_onboard_tenant(
    *,
    params: QuickOnboardParams,
    request: Request | WebSocket,
    caller: AuthenticatedCaller,
) -> OnboardingStatusResponse:
    """Run the single-call onboarding flow for the operator's own tenant.

    Creates the tenant record, confirms consent, records offshore-inference consent,
    and optionally enables the voice channel. Requires CallerRole.OPERATOR, and the
    caller's token ``tid`` must match the ``tenantId`` being onboarded (this is the
    operator's own tenant, not a customer tenant).

    If any step fails, the earlier steps are not rolled back (Cosmos does not support
    multi-document transactions). The returned status shows exactly what succeeded and
    what remains, so the caller (REST or voice) can resume with the individual
    endpoints. The shared function raises :class:`HTTPException`/``AuthorizationError``
    for domain failures; each caller maps those to its own response shape.
    """
    caller.require_role(CallerRole.OPERATOR)

    # The operator's own tenant must match the tenantId being onboarded.
    if caller.tenant_id != params.tenant_id:
        raise HTTPException(
            status_code=403,
            detail=(
                "quick-onboard is only available for the operator's own tenant "
                f"(caller tid={caller.tenant_id}, requested tenantId={params.tenant_id})"
            ),
        )

    tenant_repository = request.app.state.tenant_repository
    now = _now(request)

    # Step 1: Create tenant record
    tenant = CustomerTenant(
        tenant_id=params.tenant_id,
        display_name=params.display_name,
        consent_state=ConsentState.PENDING,
        approved_regions=params.approved_regions,
        data_residency_regions=params.data_residency_regions,
        concurrency_cap=request.app.state.settings.governance.default_tenant_concurrency_cap,
    )
    try:
        await tenant_repository.create(params.tenant_id, tenant)
    except CosmosResourceExistsError:
        # Tenant already exists — read it and continue from its current state.
        existing = await tenant_repository.read(params.tenant_id, params.tenant_id)
        if existing is not None:
            tenant = existing

    # Step 2: Confirm consent (flip PENDING -> GRANTED)
    if tenant.consent_state is ConsentState.PENDING:
        tenant = tenant.model_copy(
            update={
                "consent_state": ConsentState.GRANTED,
                "consent_granted_at": now,
                "consent_confirmed_by_object_id": caller.object_id,
                "consent_confirmed_by_display_name": caller.display_name,
                "consent_confirmation_note": params.consent_note,
            }
        )
        tenant = CustomerTenant.model_validate(tenant.model_dump())
        tenant = await tenant_repository.replace(params.tenant_id, tenant)

    # Step 3: Record offshore-inference consent
    if tenant.offshore_inference_consent is None:
        consent_store: OffshoreInferenceConsentStore = request.app.state.consent_store
        offshore_consent = await consent_store.record(
            consent_id=str(uuid.uuid4()),
            consenting_identity_object_id=caller.object_id,
            consenting_identity_display_name=caller.display_name,
            disclosure_version=CURRENT_DISCLOSURE_VERSION,
            now=now,
        )
        tenant = tenant.model_copy(update={"offshore_inference_consent": offshore_consent})
        tenant = CustomerTenant.model_validate(tenant.model_dump())
        tenant = await tenant_repository.replace(params.tenant_id, tenant)

    # Step 4: Enable voice (if requested)
    if params.voice_enabled and not tenant.voice_channel_enabled:
        if params.voice_note is None:
            raise HTTPException(
                status_code=400,
                detail="voiceNote is required when voiceEnabled is true",
            )
        tenant = tenant.model_copy(update={"voice_channel_enabled": True})
        tenant = CustomerTenant.model_validate(tenant.model_dump())
        tenant = await tenant_repository.replace(params.tenant_id, tenant)

    # Return the full onboarding status
    return _build_onboarding_status(tenant)


@router.post("/onboarding/quick-onboard", status_code=201)
async def quick_onboard(
    body: QuickOnboardRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    """Single-call onboarding for an operator's own dev tenant.

    Creates the tenant record, confirms consent, records offshore-inference consent,
    and optionally enables the voice channel. Requires CallerRole.OPERATOR. The
    caller's token ``tid`` must match the ``tenantId`` being onboarded (this is the
    operator's own tenant, not a customer tenant).

    If any step fails, the earlier steps are not rolled back (Cosmos does not support
    multi-document transactions). The response includes the onboarding status so the
    caller can see exactly what succeeded and what remains.
    """
    params = QuickOnboardParams(
        tenant_id=body.tenant_id,
        display_name=body.display_name,
        approved_regions=body.approved_regions,
        data_residency_regions=body.data_residency_regions,
        consent_note=body.consent_note,
        voice_enabled=body.voice_enabled,
        voice_note=body.voice_note,
    )
    status = await quick_onboard_tenant(params=params, request=request, caller=caller)
    return status.model_dump(by_alias=True)
