"""The ``devops_project`` stage (T076, T076b, part of T076a; FR-038): create the customer's Azure
DevOps project, push the platform release source into its default repository, create the platform
pipeline, and configure a variable group and deployment environment.

**Scope note.** ``blueprint.yaml`` declares exactly one Azure-DevOps-related stage
(``devops_project``) and exactly one matching ``requiredPermissions`` entry (``Project
Administrator``). T076a and T076b (``devops_pipelines.py``, ``devops_environments.py``) are not
separate Sequencer stages — they are helper modules this stage's own ``execute()`` calls, under this
one stage's single audit/stage record, each doing its own check-before-write for its own
sub-resource. This includes ``devops_pipelines.py``'s service-connection creation: FR-006a's
bootstrap identity (``api/tenants.py``'s ``bootstrap_identity`` route) now exists *before* any
deployment starts, so the client id ``ensure_service_connection`` needs is already on the tenant's
``SubscriptionEntitlement`` by the time this stage runs — the sequencing blocker that once pushed
this into the (now verification-only) ``identity`` stage no longer applies. See ``identity.py``'s
own module docstring for how that stage's scope narrowed once bootstrap took over.

**What "the platform repository" is** — a decision taken directly with the product owner during
this build, not inferred: ``infra/blueprints/<blueprint_id>/`` in *this* repository is the release
source. There is no separate packaging step and no external repository URL. This stage walks that
directory and pushes its files as the new project's default repository's first commit.

Auth reuses the pattern already established and live-verified in
``groundwork_shared.validation.checks.devops``: the well-known Azure DevOps Entra resource id
``499b84ac-1321-427f-aa17-267ca6975798``, a token from the deployment's own workload identity
credential, no PAT — the secretless-identity rule. There is no official async Microsoft SDK for the
Azure DevOps REST API, so this calls the documented REST surface directly with ``httpx``, exactly
as the readiness check does.

Verified 2026-07-31 against learn.microsoft.com/rest/api/azure/devops (api-version 7.1):

- ``POST {org}/_apis/projects`` is asynchronous — it returns 202 and an ``OperationReference``
  (``id``, ``status``, ``url``), never the project itself. The project is only usable once that
  operation's own ``status`` reaches ``succeeded`` (``GET {org}/_apis/operations/{operationId}``).
- A newly created project auto-creates exactly one default Git repository, sharing the project's
  own name.
- An empty repository (no commits yet) has no ``defaultBranch`` field at all — present once, and
  only once, a commit has landed. This is the idempotence/forward-fix signal this stage uses: a
  project can exist from a prior partial attempt with its repository still empty, and that must
  still be pushed to, not skipped.
- The first commit into an empty repository uses ``refUpdates[0].oldObjectId`` of forty ``0``
  characters (confirmed from the docs' own "create a new branch" example) — there is no real parent
  commit to reference yet.

**Idempotence (FR-029):** the repository's ``defaultBranch`` presence is the single source of
truth. Present -> already pushed -> ``SKIPPED_CONVERGED``/``NO_OP``, regardless of whether the
project itself pre-existed. Absent -> never successfully pushed -> push proceeds ->
``SUCCEEDED``/``APPLIED``, whether or not the project pre-existed. This deliberately does not
compare file contents: a customer or a later Groundwork release changing the platform source is a
new blueprint version, not a same-version drift this stage is responsible for reconciling.

Domain-level failures (the creation operation reporting ``failed``/``cancelled``, a project missing
its expected default repository) raise :class:`DevOpsProjectStageError`. HTTP-level failures raise
via ``httpx``'s own ``raise_for_status``. Neither is caught here — ``Sequencer._run_one_stage``
already converts any raised exception into a halted, fully-recorded ``FAILED`` outcome (see that
module's docstring), so duplicating that handling in every stage would be the exact kind of
defensive code this codebase avoids.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.devops_environments import DevOpsEnvironmentsConfigurer
from groundwork_orchestrator.stages.devops_pipelines import (
    DevOpsPipelinesConfigurer,
    rollback_pipeline_name,
)
from groundwork_orchestrator.stages.identity import service_connection_name
from groundwork_orchestrator.stages.infrastructure import PLATFORM_SOURCE_ROOT
from groundwork_orchestrator.state.repositories import CustomerTenantRepository

logger = logging.getLogger(__name__)


async def _ensure_sc_fic_via_msi(
    credential: AsyncTokenCredential,
    subscription_id: str,
    identity_resource_id: str,
    endpoint_id: str,
    issuer: str,
    subject: str,
) -> bool:
    """Default :attr:`DevOpsProjectStage.ensure_sc_fic` implementation — ``azure-mgmt-msi``'s
    native federated-identity-credential operations (adopted 2026-08-25 in place of a
    hand-rolled ARM REST call, per use-the-SDK-where-one-exists)."""
    import re

    from azure.core.exceptions import ResourceNotFoundError

    # Same versioned model module the aio client binds — pyright otherwise resolves the
    # top-level re-export to a union that mismatches the operation's parameter type.
    from azure.mgmt.msi.v2024_11_30.aio import ManagedServiceIdentityClient
    from azure.mgmt.msi.v2024_11_30.models import FederatedIdentityCredential

    match = re.search(
        r"/resourceGroups/(?P<rg>[^/]+)/providers/Microsoft.ManagedIdentity"
        r"/userAssignedIdentities/(?P<name>[^/]+)$",
        identity_resource_id,
    )
    if match is None:
        raise DevOpsProjectStageError(
            f"bootstrap_identity_resource_id {identity_resource_id!r} is not a "
            f"user-assigned managed identity resource id"
        )
    resource_group = match.group("rg")
    identity_name = match.group("name")
    credential_name = f"fc-sc-{endpoint_id}"

    async with ManagedServiceIdentityClient(credential, subscription_id) as msi_client:
        try:
            existing = await msi_client.federated_identity_credentials.get(
                resource_group_name=resource_group,
                resource_name=identity_name,
                federated_identity_credential_resource_name=credential_name,
            )
            if (
                existing.issuer == issuer
                and existing.subject == subject
                and existing.audiences == ["api://AzureADTokenExchange"]
            ):
                return False
        except ResourceNotFoundError:
            pass

        await msi_client.federated_identity_credentials.create_or_update(
            resource_group_name=resource_group,
            resource_name=identity_name,
            federated_identity_credential_resource_name=credential_name,
            parameters=FederatedIdentityCredential(
                issuer=issuer,
                subject=subject,
                audiences=["api://AzureADTokenExchange"],
            ),
            content_type="application/json",
        )
        return True


AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
API_VERSION = "7.1"
PIPELINE_PERMISSIONS_API_VERSION = "7.2-preview.1"
# The "Basic" process template, from the Projects - Create documentation's own worked example.
DEFAULT_PROCESS_TEMPLATE_ID = "6b724908-ef14-45cf-84f8-768b5384da45"
_EMPTY_TREE_OBJECT_ID = "0" * 40


class DevOpsProjectStageError(Exception):
    """A domain-level failure this stage recognised by name — not an HTTP error, not a bug here."""


def deployment_project_name(subscription_id: str) -> str:
    """Deterministic Azure DevOps project name for a deployment target.

    Mirrors ``naming.py``'s ``deployment_resource_group_name`` convention (same subscription-id
    prefix length) so the two are trivially correlated by a human reading either name, without
    colliding with it — this names a DevOps project, not a resource group.
    """
    return f"groundwork-{subscription_id[:8]}"


def blueprint_source_hash(blueprint_dir: Path) -> str:
    """Return a deterministic SHA-256 over the blueprint file set Groundwork pushes."""
    digest = hashlib.sha256()
    for file_path in sorted(path for path in blueprint_dir.rglob("*") if path.is_file()):
        relative = file_path.relative_to(blueprint_dir).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class DevOpsProjectStage:
    """Implements ``Stage`` for the blueprint's ``devops_project`` stage."""

    def __init__(
        self,
        *,
        customer_tenant_repository: CustomerTenantRepository,
        organization_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 2.0,
        max_poll_attempts: int = 30,
        ensure_sc_fic: Callable[..., Awaitable[bool]] | None = None,
    ) -> None:
        # Optional: per-tenant engagement data (CustomerTenant.devops_organization_url) arrives on
        # StageExecutionContext and takes precedence; this constructor value is the worker-wide
        # fallback (OrchestratorSettings.devops_organization_url). Neither present -> execute()
        # raises a named error; this stage never guesses a target for a customer's project.
        self._organization_url = organization_url.rstrip("/") if organization_url else None
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts
        self._tenant_repository = customer_tenant_repository
        self._ensure_sc_fic: Callable[..., Awaitable[bool]] = (
            ensure_sc_fic or _ensure_sc_fic_via_msi
        )
        # T076a and T076b, both wired into this stage's own execute() rather than registered as
        # separate Sequencer stages.
        self._pipelines = DevOpsPipelinesConfigurer(http_client=self._client)
        self._environments = DevOpsEnvironmentsConfigurer(http_client=self._client)

    def _resolve_organization_url(self, context: StageExecutionContext) -> str:
        organization_url = context.devops_organization_url or self._organization_url
        if organization_url is None:
            raise DevOpsProjectStageError(
                "no Azure DevOps organization URL for this deployment: neither the tenant record "
                "nor the worker-wide GROUNDWORK_DEVOPS_ORGANIZATION_URL setting provides one"
            )
        return organization_url

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        organization_url = self._resolve_organization_url(context)
        project_name = deployment_project_name(context.plan.subscription_id)
        headers = await self._headers(context.credential)

        project = await self._find_project(organization_url, project_name, headers)
        if project is None:
            project = await self._create_project(organization_url, project_name, headers)

        repository = await self._find_repository(
            organization_url, project_name, project_name, headers
        )
        if repository is None:
            raise DevOpsProjectStageError(
                f"project {project_name!r} has no default repository named the same as the "
                f"project; Azure DevOps did not auto-create one as expected"
            )

        push_applied = False
        pushed: tuple[str, ...] = ()
        if not repository.get("defaultBranch"):
            pushed = await self._push_platform_source(
                organization_url, project_name, repository["id"], context.plan.blueprint_id, headers
            )
            push_applied = True

        # T076a (pipeline half) / T076b: each does its own check-before-write, independent of
        # whether the repository push above was itself a no-op — a prior attempt may have pushed
        # the repository and then failed before reaching these, and that must still forward-fix
        # (matching every other stage's idempotence discipline in this codebase), not be skipped
        # just because the repository push converged.
        pipeline_applied, pipeline, pipeline_id = await self._pipelines.ensure_pipeline(
            organization_url=organization_url,
            project_name=project_name,
            repository_id=repository["id"],
            repository_name=repository["name"],
            headers=headers,
        )
        (
            rollback_pipeline_applied,
            rollback_pipeline,
            rollback_pipeline_id,
        ) = await self._pipelines.ensure_pipeline(
            organization_url=organization_url,
            project_name=project_name,
            repository_id=repository["id"],
            repository_name=repository["name"],
            headers=headers,
            pipeline_name_override=rollback_pipeline_name(project_name),
            yaml_path="/rollback.yml",
        )
        variable_group_applied, variable_group_id = await self._environments.ensure_variable_group(
            organization_url=organization_url,
            project_id=project["id"],
            project_name=project_name,
            subscription_id=context.plan.subscription_id,
            headers=headers,
        )
        (
            environment_applied,
            environment,
            environment_id,
        ) = await self._environments.ensure_environment(
            organization_url=organization_url,
            project_name=project_name,
            headers=headers,
        )

        managed_identity_client_id = await self._resolve_bootstrap_identity_client_id(context)
        (
            connection_applied,
            connection,
            connection_endpoint,
        ) = await self._pipelines.ensure_service_connection(
            organization_url=organization_url,
            project_id=project["id"],
            project_name=project_name,
            connection_name=service_connection_name(context.plan.subscription_id),
            subscription_id=context.plan.subscription_id,
            tenant_id=context.deployment.tenant_id,
            managed_identity_client_id=managed_identity_client_id,
            headers=headers,
        )

        # Found live 2026-08-25: Azure DevOps now defaults new WorkloadIdentityFederation
        # connections to the Microsoft Entra issuer flavour, whose subject embeds the
        # server-assigned endpoint id — bootstrap's predicted vstoken credential can never match
        # it (AADSTS700211 on the pipeline's first az login). The authoritative issuer/subject
        # are whatever ADO wrote onto the endpoint document itself; mirror them into a federated
        # credential on the bootstrap identity right here. Idempotent: an existing credential
        # with identical values is left untouched.
        federation_applied = False
        if connection_endpoint is not None:
            federation = self._pipelines.federation_parameters(connection_endpoint)
            if federation is not None:
                federation_applied = await self._ensure_sc_federated_credential(
                    context, str(connection_endpoint["id"]), *federation
                )

        # Second live-found gap: a newly created service connection/environment is not usable by
        # the pipeline until authorized for it — the run parks at Checkpoint.Authorization until
        # the stage's poll budget dies. Authorize this pipeline for both resources explicitly.
        authorization_applied = await self._authorize_pipeline_resources(
            context,
            organization_url=organization_url,
            project_name=project_name,
            headers=headers,
            pipeline_ids=(pipeline_id, rollback_pipeline_id),
            endpoint_id=str(connection_endpoint["id"]) if connection_endpoint else None,
            environment_id=environment_id,
        )

        applied = (
            push_applied
            or pipeline_applied
            or rollback_pipeline_applied
            or variable_group_applied
            or environment_applied
            or connection_applied
            or federation_applied
            or authorization_applied
        )
        return StageOutcome(
            status=StageStatus.SUCCEEDED,
            idempotence_outcome=(
                IdempotenceOutcome.APPLIED if applied else IdempotenceOutcome.NO_OP
            ),
            resources_affected=(
                project_name,
                *pushed,
                pipeline,
                rollback_pipeline,
                variable_group_id,
                environment,
                connection,
            ),
        )

    async def _bootstrap_entitlement(self, context: StageExecutionContext):
        """The subscription's ``SubscriptionEntitlement``, or a named error when the tenant was
        never bootstrapped (FR-006a) — shared by every bootstrap-identity consumer in this
        stage (client id for the service connection, resource id for its federated credential).
        """
        tenant = await self._tenant_repository.read(
            context.deployment.tenant_id, context.deployment.tenant_id
        )
        entitlement = tenant.entitlement_for(context.plan.subscription_id) if tenant else None
        if entitlement is None or entitlement.bootstrap_identity_client_id is None:
            raise DevOpsProjectStageError(
                f"subscription {context.plan.subscription_id} has no bootstrap identity on "
                f"record; run the tenant's bootstrap-identity onboarding step before deploying"
            )
        return entitlement

    async def _resolve_bootstrap_identity_client_id(self, context: StageExecutionContext) -> str:
        """FR-006a: the bootstrap identity is created once, per subscription, at onboarding time
        (``api/tenants.py``'s ``bootstrap_identity`` route) — long before this deployment starts.
        A subscription queued for deployment without ever being bootstrapped is a real, addressable
        gap (the operator needs to run onboarding first), not something this stage can paper over
        by guessing or skipping the service connection."""
        entitlement = await self._bootstrap_entitlement(context)
        client_id = entitlement.bootstrap_identity_client_id
        return str(client_id)

    async def _ensure_sc_fic(
        self,
        credential: AsyncTokenCredential,
        subscription_id: str,
        identity_resource_id: str,
        endpoint_id: str,
        issuer: str,
        subject: str,
    ) -> bool:
        """Default :data:`_EnsureScFic` implementation — ``azure-mgmt-msi``'s native
        federated-identity-credential operations (added 2026-08-25, replacing a hand-rolled
        ARM REST call per this codebase's use-the-SDK-where-one-exists rule)."""
        import re

        from azure.core.exceptions import ResourceNotFoundError

        # Same versioned model module the aio client binds — pyright otherwise resolves the
        # top-level re-export to a union that mismatches the operation's parameter type.
        from azure.mgmt.msi.v2024_11_30.aio import ManagedServiceIdentityClient
        from azure.mgmt.msi.v2024_11_30.models import FederatedIdentityCredential

        match = re.search(
            r"/resourceGroups/(?P<rg>[^/]+)/providers/Microsoft.ManagedIdentity"
            r"/userAssignedIdentities/(?P<name>[^/]+)$",
            identity_resource_id,
        )
        if match is None:
            raise DevOpsProjectStageError(
                f"bootstrap_identity_resource_id {identity_resource_id!r} is not a "
                f"user-assigned managed identity resource id"
            )
        resource_group = match.group("rg")
        identity_name = match.group("name")
        credential_name = f"fc-sc-{endpoint_id}"

        async with ManagedServiceIdentityClient(credential, subscription_id) as msi_client:
            try:
                existing = await msi_client.federated_identity_credentials.get(
                    resource_group_name=resource_group,
                    resource_name=identity_name,
                    federated_identity_credential_resource_name=credential_name,
                )
                if (
                    existing.issuer == issuer
                    and existing.subject == subject
                    and existing.audiences == ["api://AzureADTokenExchange"]
                ):
                    return False
            except ResourceNotFoundError:
                pass

            await msi_client.federated_identity_credentials.create_or_update(
                resource_group_name=resource_group,
                resource_name=identity_name,
                federated_identity_credential_resource_name=credential_name,
                parameters=FederatedIdentityCredential(
                    issuer=issuer,
                    subject=subject,
                    audiences=["api://AzureADTokenExchange"],
                ),
                content_type="application/json",
            )
            return True

    async def _headers(self, credential: AsyncTokenCredential) -> dict[str, str]:
        token = await credential.get_token(f"{AZURE_DEVOPS_RESOURCE_ID}/.default")
        return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}

    async def _ensure_sc_federated_credential(
        self,
        context: StageExecutionContext,
        endpoint_id: str,
        issuer: str,
        subject: str,
    ) -> bool:
        """Mirror the service connection's effective issuer/subject into a federated credential
        on the bootstrap identity. Returns ``True`` only when a credential was created or
        changed — an existing identical credential is a no-op.

        The bootstrap-time credential (``identity.py``) predicts the legacy vstoken flavour;
        this one covers whatever flavour ADO actually chose, keyed deterministically by the
        endpoint id so it never collides with the bootstrap credential's name.
        """
        entitlement = await self._bootstrap_entitlement(context)
        resource_id = entitlement.bootstrap_identity_resource_id
        if resource_id is None:
            raise DevOpsProjectStageError(
                "bootstrap identity is on record without a resource id; "
                "re-run the tenant's bootstrap-identity onboarding step"
            )
        return await self._ensure_sc_fic(
            context.credential,
            context.plan.subscription_id,
            resource_id,
            endpoint_id,
            issuer,
            subject,
        )

    async def _authorize_pipeline_resources(
        self,
        context: StageExecutionContext,
        *,
        organization_url: str,
        project_name: str,
        headers: dict[str, str],
        pipeline_ids: tuple[str, ...],
        endpoint_id: str | None,
        environment_id: str | None,
    ) -> bool:
        """Grant this deployment's pipeline access to its own service connection and environment.

        Both are ADO *protected resources*: without explicit authorization the run parks at
        ``Checkpoint.Authorization`` until the stage's poll budget expires. PATCH merges the
        authorized-pipeline list server-side, so re-granting is safe on every attempt.
        Returns ``True`` when at least one grant was actually added.
        """
        applied = False
        resource_ids = (
            ("endpoint", endpoint_id),
            ("environment", environment_id),
        )
        for resource_type, resource_id in resource_ids:
            if not resource_id:
                continue
            url = (
                f"{organization_url}/{project_name}/_apis/pipelines/pipelinepermissions"
                f"/{resource_type}/{resource_id}?api-version={PIPELINE_PERMISSIONS_API_VERSION}"
            )
            before = await self._client.get(url, headers=headers)
            authorized_pipeline_ids: set[int] = set()
            if before.status_code == 200:
                for entry in before.json().get("pipelines", []):
                    if entry.get("authorized"):
                        authorized_pipeline_ids.add(int(entry.get("id", -1)))
            missing_ids = tuple(
                pipeline_id
                for pipeline_id in pipeline_ids
                if int(pipeline_id) not in authorized_pipeline_ids
            )
            if not missing_ids:
                continue
            body = {
                "pipelines": [
                    {"id": int(pipeline_id), "authorized": True} for pipeline_id in missing_ids
                ]
            }
            response = await self._client.patch(url, headers=headers, json=body)
            response.raise_for_status()
            applied = True
        return applied

    async def _find_project(
        self, organization_url: str, name: str, headers: dict[str, str]
    ) -> dict[str, Any] | None:
        url = f"{organization_url}/_apis/projects?api-version={API_VERSION}"
        response = await self._client.get(url, headers=headers)
        response.raise_for_status()
        for project in response.json()["value"]:
            if project["name"] == name:
                return project
        return None

    async def _create_project(
        self, organization_url: str, name: str, headers: dict[str, str]
    ) -> dict[str, Any]:
        url = f"{organization_url}/_apis/projects?api-version={API_VERSION}"
        body = {
            "name": name,
            "description": (
                "Groundwork-managed data platform project. Release source: "
                "infra/blueprints/ in the Groundwork platform repository."
            ),
            "capabilities": {
                "versioncontrol": {"sourceControlType": "Git"},
                "processTemplate": {"templateTypeId": DEFAULT_PROCESS_TEMPLATE_ID},
            },
        }
        response = await self._client.post(url, headers=headers, json=body)
        response.raise_for_status()
        operation = response.json()

        await self._wait_for_operation(operation["url"], headers)

        project = await self._find_project(organization_url, name, headers)
        if project is None:
            raise DevOpsProjectStageError(
                f"project {name!r} creation operation reported success but the project is not "
                f"listed"
            )
        return project

    async def _wait_for_operation(self, operation_url: str, headers: dict[str, str]) -> None:
        url = (
            operation_url
            if "api-version" in operation_url
            else f"{operation_url}?api-version={API_VERSION}"
        )
        for _ in range(self._max_poll_attempts):
            response = await self._client.get(url, headers=headers)
            response.raise_for_status()
            status = response.json()["status"]
            if status == "succeeded":
                return
            if status in ("failed", "cancelled"):
                raise DevOpsProjectStageError(
                    f"Azure DevOps project-creation operation ended {status!r}"
                )
            await asyncio.sleep(self._poll_interval_seconds)
        raise DevOpsProjectStageError(
            "Azure DevOps project-creation operation did not complete within the poll budget"
        )

    async def _find_repository(
        self,
        organization_url: str,
        project_name: str,
        repository_name: str,
        headers: dict[str, str],
    ) -> dict[str, Any] | None:
        url = f"{organization_url}/{project_name}/_apis/git/repositories?api-version={API_VERSION}"
        response = await self._client.get(url, headers=headers)
        response.raise_for_status()
        for repository in response.json()["value"]:
            if repository["name"] == repository_name:
                return repository
        return None

    async def _push_platform_source(
        self,
        organization_url: str,
        project_name: str,
        repository_id: str,
        blueprint_id: str,
        headers: dict[str, str],
    ) -> tuple[str, ...]:
        source_dir = PLATFORM_SOURCE_ROOT / blueprint_id
        if not source_dir.is_dir():
            raise DevOpsProjectStageError(
                f"no platform source found for blueprint {blueprint_id!r} at {source_dir}"
            )

        source_hash = blueprint_source_hash(source_dir)

        changes: list[dict[str, Any]] = []
        affected: list[str] = []
        for file_path in sorted(source_dir.rglob("*")):
            if not file_path.is_file():
                continue
            relative = file_path.relative_to(source_dir).as_posix()
            changes.append(
                {
                    "changeType": "add",
                    "item": {"path": f"/{relative}"},
                    "newContent": {
                        "content": file_path.read_text(encoding="utf-8"),
                        "contentType": "rawtext",
                    },
                }
            )
            affected.append(relative)

        if not changes:
            raise DevOpsProjectStageError(
                f"platform source directory {source_dir} contains no files to push"
            )

        url = (
            f"{organization_url}/{project_name}/_apis/git/repositories/{repository_id}"
            f"/pushes?api-version={API_VERSION}"
        )
        body = {
            "refUpdates": [{"name": "refs/heads/main", "oldObjectId": _EMPTY_TREE_OBJECT_ID}],
            "commits": [
                {
                    "comment": (
                        f"Groundwork: initial platform source ({blueprint_id}, {source_hash})"
                    ),
                    "changes": changes,
                }
            ],
        }
        response = await self._client.post(url, headers=headers, json=body)
        response.raise_for_status()
        logger.info(
            "blueprint_pushed",
            extra={
                "component": "orchestrator",
                "operation": "devops_project",
                "blueprint_id": blueprint_id,
                "blueprint_source_hash": source_hash,
                "repository_id": repository_id,
            },
        )
        return tuple(affected)
