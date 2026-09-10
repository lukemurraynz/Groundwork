"""The ``identity`` stage (T080; FR-006, FR-009, FR-038): create the one federated identity
credential the platform's already-created managed identity needs — the secretless trust anchor a
not-yet-built Azure DevOps "Workload identity federation" service connection (T076a) will consume.

**What ``main.bicep`` already covers.** The infrastructure stage's single deployment stack (T077)
already provisions the platform's one user-assigned managed identity through the pinned AVM module
(``avm/res/managed-identity/user-assigned-identity:0.4.0``), at the deterministic name
``uami-gw-{resourceToken}`` — a literal authored directly in ``main.bicep``, not an AVM-internal
computed name. There is no second identity to create here; doing so would duplicate the
infrastructure stage's own write, exactly the mistake the ``networking`` stage's investigation
(T079) avoided for private endpoints.

**What is genuinely left, read from ``blueprint.yaml`` itself.** The ``identity`` stage's
``requiredPermissions`` justification is the authoritative scope statement, and it is narrower than
its own ``idempotenceContract`` prose (which still talks about "role assignments" — stale language
predating this stage's real scope being worked out, the same kind of drift the ``networking``
stage's blueprint entry had before T079, and just as deliberately not corrected here — that is
``blueprint.yaml`` maintenance for the calling session, not this file):

    Creating user-assigned identities and federated credentials for the deployed platform.
    Deliberately not Owner: no role assignment authority is needed at this stage.

The identity half is done (above). The federated-credential half is not: ``main.bicep`` composes
exactly six AVM modules and none creates a
``Microsoft.ManagedIdentity/userAssignedIdentities/federatedIdentityCredentials`` child resource.
FR-006 requires workload identity federation, never a long-lived secret. FR-038 requires the Azure
DevOps project this platform already creates (T076) to eventually gain "pipelines, service
connections, variable groups, and deployment environments" (T076a/T076b — not yet built, see
`the project's internal implementation notes (not included in this release)`).
Azure DevOps's own "Workload identity federation" Azure Resource Manager service connection type
is exactly the consumer FR-006 already commits this platform to: the service connection presents
an Entra-issued OIDC token, and Azure trusts it *only* because a
federated credential naming that exact issuer/subject/audience already exists on the managed
identity. Creating the federated credential does not require the service connection (or T076a) to
exist first — the two sides of a federated-credential trust can be created in either order, since
Azure never validates the external resource at credential-creation time. This stage creates that
trust anchor; T076a later creates the Azure DevOps-side service connection that presents it.

**Why ``Managed Identity Contributor`` at resource-group scope is exactly sufficient — verified
live, not assumed.** ``az role definition list --name "Managed Identity Contributor"``
(2026-08-01) shows its full action list is
``Microsoft.ManagedIdentity/userAssignedIdentities/{read,write,delete}``, the matching
``federatedIdentityCredentials/{read,write,delete}``, ``revokeTokens/action``, and a handful of
unrelated read-only/support actions — no ``Microsoft.Authorization/roleAssignments/write`` anywhere
in it. That is precisely this stage's required surface: read and write one
``federatedIdentityCredentials`` child resource. Nothing here needs, or could use, role-assignment
authority.

**Why this stage never reads the infrastructure stage's deployment stack (unlike**
**``networking.py``).** The Key Vault's private endpoint name is computed internally by its AVM
module and is genuinely undocumented, which is why ``networking.py`` resolves it by reading the
deployment stack's own managed-resource list rather than guessing. The managed identity's name is
not like that — ``main.bicep`` authors it directly as a literal
(``name: 'uami-gw-${resourceToken}'``) built from the same ``resource_token`` function
``infrastructure.py`` already exposes, exactly as deterministic as that module's own
``deployment_stack_name`` and ``deployment_resource_group_name``. This stage reuses those three
functions unchanged and addresses the managed identity's federated-credential child resource
directly by its computed resource ID. This also matches the permission grant: resource-group-scoped
``Managed Identity Contributor`` cannot read the subscription-scoped deployment stack
``networking.py`` reads, and does not need to, because nothing here depends on an AVM-internal
name.

**The federated credential's issuer, subject, and audience — verified, not invented.** Verified live
2026-08-01 against the versioned TypeSpec/OpenAPI source
(``gh api repos/Azure/azure-rest-api-specs/contents/specification/msi/resource-manager/
Microsoft.ManagedIdentity/ManagedIdentity/{models.tsp,examples/2024-11-30/
FederatedIdentityCredentialCreate.json}`` — the same versioned-source workaround
``.apm/known-pitfalls.md`` records for Microsoft Learn's unreliable ``?view=`` monikers, applied
here because this session's own Learn fetches for the Connection Data and federated-credential
pages 404'd), cross-checked against the live-supported API versions (``az provider show --namespace
Microsoft.ManagedIdentity``, which lists ``2024-11-30`` as the newest stable, non-preview version —
used here rather than the newer ``2025-05-31-PREVIEW``, which is not appropriate for a production
path):

- ``PUT .../userAssignedIdentities/{name}/federatedIdentityCredentials/{name}
  ?api-version=2024-11-30`` is a *synchronous* create-or-replace (``ArmResourceCreateOrReplaceSync``
  in the TypeSpec source) — no polling required, unlike the Deployment Stacks and Azure DevOps
  operation APIs the sibling stages call. Body: ``{"properties": {"issuer": <url>, "subject":
  <string>, "audiences": [<string>]}}``; response is ``200`` or ``201`` with the same shape.
- Microsoft's own worked example uses ``"audiences": ["api://AzureADTokenExchange"]`` — the fixed,
  documented OIDC token-exchange audience Entra ID federated credentials use industry-wide (the
  same value GitHub Actions and AKS workload identity federation both use), confirmed as the value
  Azure DevOps workload identity federation also expects (Microsoft DevOps Blog, "Introduction to
  Azure DevOps Workload identity federation (OIDC) with Terraform").
- Azure DevOps's own subject format for a "Workload identity federation (manual)" Azure Resource
  Manager service connection is ``sc://{organizationName}/{projectName}/{serviceConnectionName}``,
  and its issuer is ``https://vstoken.dev.azure.com/{organizationId}`` — critically the
  organisation's **GUID**, not its name (confirmed independently across the Azure DevOps blog post
  above and community write-ups; this is the one genuinely easy-to-get-wrong fact here, the same
  category of gotcha as the Learn-moniker pitfall). ``{organizationName}`` and ``{projectName}`` are
  cheap to get right (config and ``deployment_project_name`` respectively); ``{organizationId}`` is
  not something this stage can compute — it resolves it live via the Azure DevOps Location
  service's ``GET {organization}/_apis/connectionData`` (the same secretless Entra-token auth
  ``devops_project.py`` already uses, ``AZURE_DEVOPS_RESOURCE_ID`` reused unchanged from that
  module). Verified the response's organisation-identifying field is ``instanceId`` — not the
  ``locationServiceInstanceGuid`` name one older blog post used — by reading the wire-format
  attribute map straight from Microsoft's own generated clients (``azure-devops-python-api``'s
  ``ConnectionData`` model: ``'instance_id': {'key': 'instanceId', ...}`` — the same
  disambiguation-by-generated-client-source technique used nowhere else in this codebase yet,
  because no other stage has needed a field name two independent sources disagreed on).

**The one design element this stage had to decide that a not-yet-built stage (T076a) will need to
match, and why it is safe to decide here.** ``{serviceConnectionName}`` has no other source of
truth anywhere in this codebase — T076a, which will actually create the Azure DevOps service
connection, does not exist yet. Rather than invent a new naming scheme,
``service_connection_name`` below returns exactly ``deployment_project_name(subscription_id)`` —
the same deterministic string ``devops_project.py`` (T076) already computes and already uses as
both the Azure DevOps project name and (implicitly, via Azure DevOps's own auto-creation
behaviour) its default repository name. A service connection's name only needs to be unique within
its own project, so naming it identically to the project it lives in needs no new scheme and
cannot collide with anything else this platform creates. T076a **must** create its service
connection under this exact name, or the subject this stage mints will never match a real service
connection and the trust will silently never work; this module exports the function specifically
so T076a imports it rather than re-deriving it.

**Idempotence (FR-029):** a ``GET`` before any write, same discipline as ``infrastructure.py``.
An existing credential whose ``issuer``, ``subject``, and ``audiences`` already match the expected
values is converged — ``SKIPPED_CONVERGED``/``NO_OP``. Anything else (absent, or present with
drifted properties from an interrupted or superseded prior attempt) submits the same deterministic
``PUT`` — forward-fix falls out of the API's own create-or-replace semantics, matching
``blueprint.yaml``'s own recovery path for this stage.

**The service-connection half of T076a is wired in here, not in ``devops_project.py``.**
``devops_pipelines.py``'s own docstring documents why: creating a Manual-mode
``WorkloadIdentityFederation`` service connection needs the managed identity's client id as a
validated request parameter, and that identity does not exist until the ``infrastructure`` stage
(a *dependent* of ``devops_project`` in ``blueprint.yaml``'s DAG) has run — so ``devops_project``
itself can never correctly create it. This stage is the first point in the DAG where every
prerequisite already holds: it runs after ``infrastructure`` (client id available via
``resolve_managed_identity_client_id``, which reads the same deployment stack this stage's own
organisation-GUID resolution already talks to), and after ``devops_project`` transitively (the
Azure DevOps project this connection lives in already exists). ``tenant_id`` for
``authorization.parameters.tenantid`` comes from ``context.deployment.tenant_id`` — already the
Entra tenant this deployment targets, not something new to resolve.
"""

from __future__ import annotations

import asyncio

import httpx

from groundwork_contracts.audit import IdempotenceOutcome
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.infrastructure import (
    deployment_resource_group_name,
    resource_token,
)
from groundwork_orchestrator.stages.pipeline_execution import (
    deployment_project_name,
    latest_run,
    poll_pipeline_run,
    stage_outcome_for,
)

FEDERATED_CREDENTIAL_API_VERSION = "2024-11-30"
CONNECTION_DATA_API_VERSION = "7.1-preview"
"""Live-verified 2026-08-24: plain '7.1' 400s ("under preview... supply -preview") — this
Location-service endpoint has never shipped a stable api-version. The earlier '[VERIFIED]' claim
of '7.1' for this constant was wrong; ``connectionData`` is the one Azure DevOps call in this
codebase that is not on the stable 7.1 surface everything else uses."""
TOKEN_EXCHANGE_AUDIENCE = "api://AzureADTokenExchange"  # noqa: S105 -- an OIDC audience, not a secret


class IdentityStageError(Exception):
    """A domain-level failure this stage recognised by name — not an HTTP error, not a bug here."""


def managed_identity_name(subscription_id: str) -> str:
    """Must stay byte-identical to ``main.bicep``'s ``uami-gw-${resourceToken}`` literal, or this
    stage addresses a resource that does not exist."""
    return f"uami-gw-{resource_token(subscription_id)}"


def federated_credential_name(subscription_id: str) -> str:
    """Deterministic name for the federated credential resource itself, following the same
    ``groundwork-{subscriptionId[:8]}``-derived convention as every other stage's naming helper."""
    return f"fc-groundwork-{subscription_id[:8]}"


def service_connection_name(subscription_id: str) -> str:
    """The Azure DevOps "Workload identity federation" service connection name T076a must create.

    Deliberately reuses ``devops_project.py``'s own ``deployment_project_name`` rather than
    inventing a second naming scheme — see this module's docstring for why that is safe."""
    return deployment_project_name(subscription_id)


def _organization_name(organization_url: str) -> str:
    return organization_url.rstrip("/").rsplit("/", 1)[-1]


def _expected_subject(organization_url: str, subscription_id: str) -> str:
    return (
        f"sc://{_organization_name(organization_url)}/{deployment_project_name(subscription_id)}"
        f"/{service_connection_name(subscription_id)}"
    )


def federated_credential_resource_id(subscription_id: str) -> str:
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/"
        f"{deployment_resource_group_name(subscription_id)}/providers/Microsoft.ManagedIdentity"
        f"/userAssignedIdentities/{managed_identity_name(subscription_id)}"
        f"/federatedIdentityCredentials/{federated_credential_name(subscription_id)}"
    )


class IdentityStage:
    """Implements ``Stage`` for the blueprint's ``identity`` stage.

    **Rewritten 2026-08-24 (Clarifications, FR-038a) — real work superseded, now
    verification-only.** This stage's two real writes both moved elsewhere: the federated
    identity credential is created once, at onboarding time, by ``api/tenants.py``'s
    ``bootstrap_identity`` route (FR-006a) — before this deployment even starts, not per-attempt
    — and the Azure DevOps service connection moves to ``devops_project.py`` (T076a), since the
    bootstrap identity's client id is now known from the start instead of only appearing after
    ``infrastructure`` has run (the sequencing blocker this module's own docstring above
    describes no longer applies once bootstrap creates the identity first). Nothing is left here
    to write. This stage now only confirms the same pipeline run ``infrastructure`` triggered
    converged — same shape as ``networking.py``'s own verification-only stage, via
    ``stages/pipeline_execution.py``.

    The naming helpers above (``managed_identity_name``, ``federated_credential_name``,
    ``federated_credential_resource_id``, ``service_connection_name``, ``_expected_subject``) are
    unchanged and still load-bearing — ``api/tenants.py``'s bootstrap route imports them directly,
    so removing them would break bootstrap, not just this now-thin stage.
    """

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 120,
    ) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        organization_url = context.devops_organization_url
        if not organization_url:
            raise IdentityStageError(
                "no Azure DevOps organization URL for this deployment: neither the tenant "
                "record nor the worker-wide GROUNDWORK_DEVOPS_ORGANIZATION_URL setting "
                "provides one"
            )
        subscription_id = context.plan.subscription_id

        outcome = await latest_run(
            credential=context.credential,
            organization_url=organization_url,
            subscription_id=subscription_id,
            http_client=self._client,
        )
        if outcome is None:
            raise IdentityStageError(
                "no pipeline run found for this subscription; the infrastructure stage must "
                "have run and triggered one before this stage can verify it"
            )

        for _ in range(self._max_poll_attempts):
            if outcome.state == "completed":
                break
            await asyncio.sleep(self._poll_interval_seconds)
            outcome = await poll_pipeline_run(
                credential=context.credential,
                organization_url=organization_url,
                subscription_id=subscription_id,
                resume_token=outcome.resume_token,
                http_client=self._client,
            )
        else:
            raise IdentityStageError(
                f"pipeline run {outcome.run_id} did not reach a terminal state within the "
                f"poll budget ({self._max_poll_attempts} attempts at "
                f"{self._poll_interval_seconds}s)"
            )

        return stage_outcome_for(
            outcome, resources_affected=(), idempotence_outcome=IdempotenceOutcome.NO_OP
        )
