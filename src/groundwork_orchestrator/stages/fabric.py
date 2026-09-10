"""The ``fabric`` stage (T081; FR-036, FR-037) — trigger-and-poll of the customer's own
platform-release pipeline with ``provisionFabric=true``, decided 2026-08-25: Fabric provisions
via Azure DevOps exactly like infrastructure, never via direct ARM/Fabric calls from this
service. The pipeline's own Fabric block (``azure-pipelines.yml``'s ``provisionFabric``
step) creates the capacity with an idempotent ARM PUT, waits for ``provisioningState ==
Succeeded``, then find-or-creates the workspace and its Key Vault managed private endpoint
through ``api.fabric.microsoft.com`` using the same federated service-connection identity —
so the only credential this stage ever exercises is the Azure DevOps one every other
post-bootstrap stage already uses.

History: the previous direct-call implementation (``azure-mgmt-fabric`` + raw Fabric REST from
this service) was removed after the first live deployment reached this stage and failed; it also
carried an unresolved capacity-admin-UPN data-model gap and a [VERIFY] on app-only Fabric token
scopes. Both concerns survive the move but become *pipeline-side*: the UPN is still required
(same named-error guard as before, now passed through as a template parameter), and Fabric's
"Service principals can use Fabric APIs" tenant setting is still a customer-side prerequisite the
pipeline's curl calls fail loudly without.

The blueprint's 900-second managed-private-endpoint retry interval (``blueprint.yaml``,
research notes V-006) remains T072's job; this stage only needs to not fight it once it exists.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from groundwork_contracts.audit import IdempotenceOutcome
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.infrastructure import (
    deployment_resource_group_name,
    resource_token,
)
from groundwork_orchestrator.stages.pipeline_execution import (
    PipelineExecutionError,
    PipelineRunOutcome,
    poll_pipeline_run,
    stage_outcome_for,
    trigger_pipeline_run,
)

# The Fabric REST surface. No longer called by this stage itself (provisioning moved into the
# customer pipeline), but validation_tests.py's read-only workspace check and anything that
# inspects Fabric state still addresses the same endpoint/scope.
FABRIC_API_ENDPOINT = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def capacity_name(subscription_id: str) -> str:
    """Lowercase-alphanumeric only — Microsoft.Fabric/capacities rejects hyphens outright
    ("Invalid chars in resource name", found live 2026-08-25). Keyed on ``resource_token`` so the
    name matches exactly what the customer-pipeline Fabric step computes (the pipeline passes the
    token as a template parameter; subscription-prefix and token are different digests).
    """
    return f"fabricgw{resource_token(subscription_id)}"


def workspace_name(subscription_id: str) -> str:
    """Matches the pipeline's workspace displayName: hyphen allowed here (display name), token
    suffix shared with :func:`capacity_name` so the pair stays correlated."""
    return f"ws-groundwork-{resource_token(subscription_id)}"


def key_vault_resource_id(subscription_id: str) -> str:
    """``main.bicep``'s own literal Key Vault name — the managed private endpoint target."""
    return (
        f"/subscriptions/{subscription_id}/resourceGroups/"
        f"{deployment_resource_group_name(subscription_id)}/providers/Microsoft.KeyVault"
        f"/vaults/kv-gw-{resource_token(subscription_id)}"
    )


class FabricStageError(Exception):
    """A domain-level failure this stage recognised by name — not an HTTP error, not a bug here."""


class FabricStage:
    """Implements ``Stage`` for the blueprint's ``fabric`` stage.

    ``capacity_admin_upn`` remains required-without-default (Fabric capacity administration
    members must be a tenant UPN; nothing in the plan derives one — see the previous
    implementation's reasoning, which still holds). The per-tenant record wins over the
    worker-wide setting, exactly like ``devops_organization_url``.
    """

    def __init__(
        self,
        *,
        capacity_admin_upn: str | None = None,
        validation_principal_object_id: str | None = None,
        organization_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 120,
    ) -> None:
        self._capacity_admin_upn = capacity_admin_upn
        self._validation_principal_object_id = validation_principal_object_id
        self._organization_url = organization_url.rstrip("/") if organization_url else None
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        capacity_admin_upn = (
            context.fabric_capacity_admin_upn or self._capacity_admin_upn or ""
        ).strip()
        # Found live 2026-08-25: the k8s manifest ships this variable as the literal string
        # "<no value>" when the azd env never set it — truthy, so a naive check would pass it
        # straight into ARM and fail opaquely there instead of here, with a named cause.
        if not capacity_admin_upn or capacity_admin_upn == "<no value>":
            raise FabricStageError(
                "no Fabric capacity administrator UPN for this deployment: neither the tenant "
                "record nor the worker-wide GROUNDWORK_FABRIC_CAPACITY_ADMIN_UPN setting provides "
                "one; capacity is billable to the customer, so a value is never guessed"
            )
        organization_url = context.devops_organization_url or self._organization_url
        if not organization_url:
            raise FabricStageError(
                "no Azure DevOps organization URL for this deployment: neither the tenant "
                "record nor the worker-wide GROUNDWORK_DEVOPS_ORGANIZATION_URL setting "
                "provides one"
            )
        subscription_id = context.plan.subscription_id

        checkpoint = context.deployment.checkpoint
        if checkpoint is not None and checkpoint.stage_name == "fabric":
            prior_outcome = await self._wait_for_terminal(
                context.credential,
                organization_url,
                subscription_id,
                PipelineRunOutcome(
                    run_id=-1,
                    state="inProgress",
                    result=None,
                    resume_token=checkpoint.resume_token,
                ),
            )
            if prior_outcome.state == "completed" and prior_outcome.result == "succeeded":
                return stage_outcome_for(
                    prior_outcome,
                    resources_affected=(
                        capacity_name(subscription_id),
                        workspace_name(subscription_id),
                    ),
                    idempotence_outcome=IdempotenceOutcome.NO_OP,
                )

        template_parameters = {
            "applyChanges": True,
            "location": context.plan.region.value,
            "resourceGroupName": deployment_resource_group_name(subscription_id),
            "resourceToken": resource_token(subscription_id),
            "provisionFabric": True,
            "fabricCapacitySku": context.plan.fabric_capacity_sku.value,
            "validationPrincipalObjectId": self._validation_principal_object_id or "",
            "capacityAdminUpn": capacity_admin_upn,
        }
        outcome = await trigger_pipeline_run(
            credential=context.credential,
            organization_url=organization_url,
            subscription_id=subscription_id,
            http_client=self._client,
            template_parameters=template_parameters,
        )
        outcome = await self._wait_for_terminal(
            context.credential, organization_url, subscription_id, outcome
        )
        try:
            return stage_outcome_for(
                outcome,
                resources_affected=(
                    capacity_name(subscription_id),
                    workspace_name(subscription_id),
                ),
            )
        except PipelineExecutionError as exc:
            raise FabricStageError(str(exc)) from exc

    async def _wait_for_terminal(
        self,
        credential: Any,
        organization_url: str,
        subscription_id: str,
        outcome: Any,
    ) -> Any:
        if outcome.state == "completed":
            return outcome
        for _ in range(self._max_poll_attempts):
            await asyncio.sleep(self._poll_interval_seconds)
            outcome = await poll_pipeline_run(
                credential=credential,
                organization_url=organization_url,
                subscription_id=subscription_id,
                resume_token=outcome.resume_token,
                http_client=self._client,
            )
            if outcome.state == "completed":
                return outcome
        raise FabricStageError(
            f"pipeline run {outcome.run_id} did not reach a terminal state within the poll "
            f"budget ({self._max_poll_attempts} attempts at {self._poll_interval_seconds}s)"
        )
