"""Read-only Lighthouse + Azure DevOps onboarding facts.

Operator-facing route and conversation-facing helper for the one human-guided onboarding flow that
Groundwork cannot automate away: customer-side Azure Lighthouse delegation plus Azure DevOps org
membership for Groundwork's own identity.
"""

from __future__ import annotations

import base64
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from json import JSONDecodeError
from typing import TYPE_CHECKING, Annotated, Protocol
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from groundwork_contracts.tenant import AdoOrgAccessState, ConsentState, CustomerTenant
from groundwork_controlplane.api.auth import AuthenticatedCaller, CallerRole
from groundwork_controlplane.api.errors import (
    LighthouseOnboardingNotConfiguredError,
    TenantNotFoundError,
)
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_orchestrator.stages.identity import _organization_name
from groundwork_orchestrator.stages.infrastructure import ARM_SCOPE

if TYPE_CHECKING:
    from groundwork_shared.config.settings import Settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])

LIGHTHOUSE_TEMPLATE_URI = (
    "https://raw.githubusercontent.com/lukemurraynz/Groundwork/main/"
    "infra/lighthouse/delegation.bicep"
)
LIGHTHOUSE_PRINCIPAL_DISPLAY_NAME = "Groundwork Platform bootstrap identity"
LIGHTHOUSE_API_VERSION = "2020-02-01-preview"
SERVICE_PRINCIPAL_ENTITLEMENTS_API_VERSION = "7.1-preview.1"
PCA_GROUP_NAME = "Project Collection Administrators"


class DelegationState(StrEnum):
    PENDING = "pending"
    GRANTED = "granted"


class AccessTokenLike(Protocol):
    token: str


class CredentialLike(Protocol):
    async def get_token(self, *scopes: str, **kwargs: object) -> AccessTokenLike: ...


class AsyncHttpClientLike(Protocol):
    async def get(self, url: str, **kwargs: object) -> httpx.Response: ...

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class LighthouseConstants:
    managed_by_tenant_id: str
    principal_id: str
    principal_id_display_name: str = LIGHTHOUSE_PRINCIPAL_DISPLAY_NAME


@dataclass(frozen=True, slots=True)
class LighthouseInstructions:
    subscription_id: str
    deployment_location: str
    template_uri: str
    az_deployment_command: str
    portal_deployment_link: str
    provider_registration_command: str


@dataclass(frozen=True, slots=True)
class AzureDevOpsInstructions:
    organization_url: str | None
    principal_object_id: str
    entitlement_endpoint: str
    entitlement_request_body: Mapping[str, object]
    entitlement_instruction_text: str
    pca_instruction_text: str
    # ADO's people-picker resolves this far more reliably than principal_object_id above. None
    # only when ReadinessSettings.orchestrator_display_name itself is not configured, in which
    # case pca_instruction_text falls back to naming the object id instead.
    orchestrator_display_name: str | None = None
    # The control plane's own identity — distinct from principal_object_id (the orchestrator's
    # identity) above. Found live 2026-09-07: grant_customer_ado_org_access calls Azure DevOps
    # *as* the control plane's own identity, and a brand-new organization has never heard of it
    # either, so its entitlement call 401s with a misleading "sign in at least once" error no
    # matter what the orchestrator's own entitlement state is. None only when
    # ReadinessSettings.controlplane_principal_id itself is not configured (see that field's
    # docstring); a customer following only the orchestrator instructions above in that case
    # would stay stuck with no path forward, which is exactly the bug this fixes.
    control_plane_principal_object_id: str | None = None
    control_plane_entitlement_request_body: Mapping[str, object] | None = None
    control_plane_entitlement_instruction_text: str | None = None


@dataclass(frozen=True, slots=True)
class TenantOnboardingFacts:
    tenant_id: str
    subscription_id: str
    delegation_state: DelegationState
    lighthouse: LighthouseInstructions
    azure_devops: AzureDevOpsInstructions


def _gate(state: str, next_action: str) -> dict[str, str]:
    return {"state": state, "next_action": next_action}


def build_lighthouse_http_client(
    *, transport: httpx.AsyncBaseTransport | None = None
) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=30.0, transport=transport)


def _customer_subscription_id(tenant: CustomerTenant) -> str:
    deployable = [s.subscription_id for s in tenant.subscriptions if s.may_deploy]
    if len(deployable) == 1:
        return deployable[0]
    if not deployable and len(tenant.subscriptions) == 1:
        return tenant.subscriptions[0].subscription_id
    raise HTTPException(
        status_code=409,
        detail=(
            "tenant onboarding facts require exactly one recorded subscription; "
            "record one subscription entitlement before using this route"
        ),
    )


def _deployment_location(tenant: CustomerTenant, settings: Settings) -> str:
    approved = sorted(tenant.approved_regions)
    if approved:
        return approved[0]
    return settings.azure_location


def _decode_unverified_jwt_claims(token: str) -> dict[str, object]:
    parts = token.split(".")
    if len(parts) < 2:
        raise ValueError("token is not a JWT")
    payload = parts[1]
    padded = payload + "=" * (-len(payload) % 4)
    decoded = base64.urlsafe_b64decode(padded.encode("ascii"))
    claims = json.loads(decoded)
    if not isinstance(claims, dict):
        raise ValueError("JWT payload is not an object")
    return claims


async def _groundwork_lighthouse_constants(credential: CredentialLike) -> LighthouseConstants:
    try:
        token = await credential.get_token(ARM_SCOPE)
        claims = _decode_unverified_jwt_claims(token.token)
        tenant_id = claims.get("tid")
        principal_id = claims.get("oid")
        if not isinstance(tenant_id, str) or not tenant_id:
            raise ValueError("ARM token missing tid")
        if not isinstance(principal_id, str) or not principal_id:
            raise ValueError("ARM token missing oid")
    except (AttributeError, JSONDecodeError, TypeError, ValueError) as exc:
        raise LighthouseOnboardingNotConfiguredError() from exc
    except httpx.HTTPError as exc:
        raise LighthouseOnboardingNotConfiguredError() from exc
    return LighthouseConstants(managed_by_tenant_id=tenant_id, principal_id=principal_id)


def build_lighthouse_command(
    *,
    subscription_id: str,
    deployment_location: str,
    constants: LighthouseConstants,
    template_uri: str = LIGHTHOUSE_TEMPLATE_URI,
) -> str:
    return (
        f"az deployment sub create --subscription {subscription_id} "
        f"--location {deployment_location} "
        f"--template-uri {template_uri} --parameters "
        f"managedByTenantId={constants.managed_by_tenant_id} "
        f"principalId={constants.principal_id} "
        f'principalIdDisplayName="{constants.principal_id_display_name}"'
    )


def build_lighthouse_portal_link(
    *,
    subscription_id: str,
    constants: LighthouseConstants,
    template_uri: str = LIGHTHOUSE_TEMPLATE_URI,
) -> str:
    parameters = quote(
        json.dumps(
            {
                "managedByTenantId": {"value": constants.managed_by_tenant_id},
                "principalId": {"value": constants.principal_id},
                "principalIdDisplayName": {"value": constants.principal_id_display_name},
            },
            separators=(",", ":"),
            sort_keys=True,
        ),
        safe="",
    )
    encoded_template = quote(template_uri, safe="")
    return (
        "https://portal.azure.com/#create/Microsoft.Template"
        f"/uri/{encoded_template}/subscriptionId/{subscription_id}/parameters/{parameters}"
    )


def _ado_entitlement_endpoint(organization_url: str | None) -> str:
    if organization_url is None:
        return (
            "https://vsaex.dev.azure.com/<your-organization>/"
            f"_apis/serviceprincipalentitlements?api-version={SERVICE_PRINCIPAL_ENTITLEMENTS_API_VERSION}"
        )
    organization = _organization_name(organization_url)
    return (
        f"https://vsaex.dev.azure.com/{organization}/_apis/serviceprincipalentitlements"
        f"?api-version={SERVICE_PRINCIPAL_ENTITLEMENTS_API_VERSION}"
    )


def build_azure_devops_instructions(
    *,
    tenant: CustomerTenant,
    settings: Settings,
    organization_url: str | None = None,
) -> AzureDevOpsInstructions:
    resolved_organization_url = organization_url or tenant.devops_organization_url
    endpoint = _ado_entitlement_endpoint(resolved_organization_url)
    principal_object_id = settings.readiness.orchestrator_principal_id
    body = {
        "servicePrincipal": {
            "origin": "aad",
            "originId": principal_object_id,
            "subjectKind": "servicePrincipal",
        },
        "accessLevel": {"accountLicenseType": "stakeholder"},
    }
    control_plane_principal_object_id = settings.readiness.controlplane_principal_id
    control_plane_body = (
        {
            "servicePrincipal": {
                "origin": "aad",
                "originId": control_plane_principal_object_id,
                "subjectKind": "servicePrincipal",
            },
            "accessLevel": {"accountLicenseType": "stakeholder"},
        }
        if control_plane_principal_object_id is not None
        else None
    )
    return AzureDevOpsInstructions(
        organization_url=resolved_organization_url,
        principal_object_id=principal_object_id,
        entitlement_endpoint=endpoint,
        entitlement_request_body=body,
        entitlement_instruction_text=(
            "Azure DevOps manual step 1: a customer Azure DevOps organization owner runs the "
            "servicePrincipalEntitlements POST above so Groundwork's orchestrator identity exists "
            "as an organization user with the Stakeholder license. Use the object ID exactly as "
            "shown; do not substitute a client ID."
        ),
        orchestrator_display_name=settings.readiness.orchestrator_display_name,
        pca_instruction_text=(
            "Azure DevOps manual step 2: after that entitlement exists, the same organization "
            "owner opens Organization Settings > Permissions, selects the "
            f"'{PCA_GROUP_NAME}' group, opens its Members tab, selects Add, and searches for "
            + (
                f"'{settings.readiness.orchestrator_display_name}' by name — that resolves far "
                "more reliably in the picker than the object ID does."
                if settings.readiness.orchestrator_display_name is not None
                else f"'{principal_object_id}' (its display name is not configured on this "
                "deployment, so the object ID is the only identifier available; searching by a "
                "bare ID is less reliable in the picker than a name would be)."
            )
            + " Verified against learn.microsoft.com/azure/devops/organizations/security/"
            "change-organization-collection-level-permissions, 2026-09-07. This step cannot be "
            "automated inside Groundwork's trust boundary."
        ),
        control_plane_principal_object_id=control_plane_principal_object_id,
        control_plane_entitlement_request_body=control_plane_body,
        control_plane_entitlement_instruction_text=(
            "Azure DevOps manual step 0 (do this first): Groundwork's control plane identity "
            "calls Azure DevOps on your behalf to verify and grant access, but a brand-new "
            "organization has never heard of that identity either — the same organization owner "
            "must also entitle it (same servicePrincipalEntitlements call, this object ID "
            "instead) before step 1 can succeed. Skipping this step produces a "
            '"Please sign-in at least once" error that is really about this identity, not the '
            "orchestrator one in step 1."
            if control_plane_principal_object_id is not None
            else "Azure DevOps manual step 0 cannot be given yet: this Groundwork deployment has "
            "no GROUNDWORK_CONTROLPLANE_PRINCIPAL_ID configured. Steps 1 and 2 below will not be "
            "enough on their own — see ReadinessSettings.controlplane_principal_id."
        ),
    )


async def _delegation_state(
    *,
    subscription_id: str,
    constants: LighthouseConstants,
    credential: CredentialLike,
    http_client: AsyncHttpClientLike | None = None,
) -> DelegationState:
    try:
        token = await credential.get_token(ARM_SCOPE)
    except (AttributeError, TypeError, ValueError) as exc:
        raise LighthouseOnboardingNotConfiguredError() from exc
    except httpx.HTTPError as exc:
        raise LighthouseOnboardingNotConfiguredError() from exc

    owns_client = http_client is None
    client = http_client or build_lighthouse_http_client()
    headers = {"Authorization": f"Bearer {token.token}"}
    try:
        assignments_url = (
            "https://management.azure.com/"
            f"subscriptions/{subscription_id}/providers/Microsoft.ManagedServices/registrationAssignments"
            f"?api-version={LIGHTHOUSE_API_VERSION}"
        )
        assignments_response = await client.get(assignments_url, headers=headers)
        assignments_response.raise_for_status()
        assignments = assignments_response.json().get("value", [])
        if not isinstance(assignments, list):
            return DelegationState.PENDING

        for assignment in assignments:
            if not isinstance(assignment, dict):
                continue
            properties = assignment.get("properties")
            if not isinstance(properties, dict):
                continue
            definition_id = properties.get("registrationDefinitionId")
            if not isinstance(definition_id, str) or not definition_id:
                continue
            definition_url = (
                f"https://management.azure.com{definition_id}?api-version={LIGHTHOUSE_API_VERSION}"
            )
            definition_response = await client.get(definition_url, headers=headers)
            definition_response.raise_for_status()
            definition_properties = definition_response.json().get("properties", {})
            authorizations = definition_properties.get("authorizations", [])
            if not isinstance(authorizations, list):
                continue
            for authorization in authorizations:
                if not isinstance(authorization, dict):
                    continue
                if authorization.get("principalId") == constants.principal_id:
                    return DelegationState.GRANTED
        return DelegationState.PENDING
    finally:
        if owns_client:
            await client.aclose()


async def build_tenant_onboarding_facts(
    *,
    tenant: CustomerTenant,
    settings: Settings,
    credential: CredentialLike,
    http_client: AsyncHttpClientLike | None = None,
) -> TenantOnboardingFacts:
    subscription_id = _customer_subscription_id(tenant)
    deployment_location = _deployment_location(tenant, settings)
    constants = await _groundwork_lighthouse_constants(credential)
    lighthouse = LighthouseInstructions(
        subscription_id=subscription_id,
        deployment_location=deployment_location,
        template_uri=LIGHTHOUSE_TEMPLATE_URI,
        az_deployment_command=build_lighthouse_command(
            subscription_id=subscription_id,
            deployment_location=deployment_location,
            constants=constants,
        ),
        portal_deployment_link=build_lighthouse_portal_link(
            subscription_id=subscription_id,
            constants=constants,
        ),
        provider_registration_command=(
            f"az provider register --subscription {subscription_id} "
            "--namespace Microsoft.ManagedServices"
        ),
    )
    delegation_state = await _delegation_state(
        subscription_id=subscription_id,
        constants=constants,
        credential=credential,
        http_client=http_client,
    )
    return TenantOnboardingFacts(
        tenant_id=tenant.tenant_id,
        subscription_id=subscription_id,
        delegation_state=delegation_state,
        lighthouse=lighthouse,
        azure_devops=build_azure_devops_instructions(tenant=tenant, settings=settings),
    )


def onboarding_gates_response(
    facts: TenantOnboardingFacts, tenant: CustomerTenant
) -> dict[str, object]:
    entitlement = tenant.entitlement_for(facts.subscription_id)
    has_bootstrap_identity = (
        entitlement is not None and entitlement.bootstrap_identity_resource_id is not None
    )
    bootstrap_next_action = (
        "Bootstrap identity already exists for this subscription."
        if has_bootstrap_identity
        else (
            "Run trigger_bootstrap_identity after consent and Lighthouse delegation are verified."
        )
    )
    if entitlement is None:
        bootstrap_next_action = (
            "Record a deployable subscription entitlement for this tenant before bootstrap can run."
        )
    elif tenant.devops_organization_url is None:
        bootstrap_next_action = (
            "Record the customer's Azure DevOps organization URL before bootstrap can run."
        )

    return {
        "tenant_created": _gate(
            "created",
            "Tenant onboarding record exists. Continue with customer authorization steps.",
        ),
        "consent": _gate(
            tenant.consent_state.value,
            (
                "Consent is operator-confirmed. After the customer completes the admin-consent "
                "step,"
                "a Groundwork operator must attest the result with confirm_customer_consent."
                if tenant.consent_state is ConsentState.PENDING
                else "Consent is already confirmed."
            ),
        ),
        "lighthouse_delegation": _gate(
            facts.delegation_state.value,
            (
                "Guide the customer to run the Lighthouse command or open the portal deployment "
                "link,"
                "then re-check onboarding status."
                if facts.delegation_state is DelegationState.PENDING
                else "Lighthouse delegation is verified."
            ),
        ),
        "ado_org_access": _gate(
            tenant.ado_org_access_state.value,
            (
                "Run grant_ado_org_access. If the result says member_pending_pca, guide the "
                "customer through the Project Collection Administrators web UI step, then record "
                "the attestation."
                if tenant.ado_org_access_state is AdoOrgAccessState.PENDING
                else "Azure DevOps organization access is already attested."
            ),
        ),
        "bootstrap_identity": _gate(
            "granted" if has_bootstrap_identity else "pending",
            bootstrap_next_action,
        ),
    }


def onboarding_facts_response(
    facts: TenantOnboardingFacts, tenant: CustomerTenant
) -> dict[str, object]:
    return {
        "tenantId": facts.tenant_id,
        "subscriptionId": facts.subscription_id,
        "delegationState": facts.delegation_state.value,
        "gates": onboarding_gates_response(facts, tenant),
        "lighthouse": {
            "templateUri": facts.lighthouse.template_uri,
            "deploymentLocation": facts.lighthouse.deployment_location,
            "providerRegistrationCommand": facts.lighthouse.provider_registration_command,
            "azDeploymentCommand": facts.lighthouse.az_deployment_command,
            "portalDeploymentLink": facts.lighthouse.portal_deployment_link,
        },
        "azureDevOps": {
            "organizationUrl": facts.azure_devops.organization_url,
            "principalObjectId": facts.azure_devops.principal_object_id,
            "principalDisplayName": facts.azure_devops.orchestrator_display_name,
            "servicePrincipalEntitlementsEndpoint": facts.azure_devops.entitlement_endpoint,
            "servicePrincipalEntitlementsBody": facts.azure_devops.entitlement_request_body,
            "servicePrincipalEntitlementsInstruction": (
                facts.azure_devops.entitlement_instruction_text
            ),
            "projectCollectionAdministratorsInstruction": facts.azure_devops.pca_instruction_text,
        },
    }


@router.get("/{tenant_id}/onboarding/lighthouse")
async def get_lighthouse_onboarding(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        # WAF assessment §2.6: CallerRole.OPERATOR is a broad, non-tenant-scoped role (the same
        # trust boundary every other tenant-lookup route in this codebase already relies on —
        # narrowing it here alone would not close the same 404-vs-200 oracle available via the
        # canonical `GET /v1/tenants/{tenant_id}`). What this *can* add cheaply: a detective
        # control. A pattern of many distinct tenant_id probes from the same operator identity is
        # exactly what insider misuse or a compromised operator token looks like, and this is now
        # a signal an alert rule can actually fire on (WAF §3.3's own precedent).
        logger.warning(
            "lighthouse onboarding facts requested for unknown tenant_id=%s by operator=%s",
            tenant_id,
            caller.object_id,
        )
        raise TenantNotFoundError()
    facts = await build_tenant_onboarding_facts(
        tenant=tenant,
        settings=request.app.state.settings,
        credential=request.app.state.credential,
        http_client=getattr(request.app.state, "lighthouse_http_client", None),
    )
    return onboarding_facts_response(facts, tenant)
