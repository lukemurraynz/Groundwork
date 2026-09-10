"""The ``infrastructure`` stage (T077; FR-036, FR-039): deploy the blueprint's compiled ARM
template into the customer subscription via an Azure Deployment Stack.

**Why a deployment stack, not a plain ARM deployment.** A stack is itself an Azure resource that
tracks everything it deployed, giving Groundwork a real handle for later lifecycle operations
(update, delete) rather than an opaque `Microsoft.Resources/deployments` history entry. It is also
what `blueprint.yaml`'s own ``recovery_path`` prose assumes exists when it offers rollback for this
stage — deleting the stack (T073, not built yet) is that rollback.

**Why the template is pre-compiled ARM JSON, not raw Bicep.** The Deployment Stacks REST API's
``properties.template`` accepts ARM JSON, not Bicep — there is no server-side Bicep compilation on
this path. Rather than shell out to the `bicep`/`az` CLI at runtime (a container dependency this
codebase has no other reason to carry, and a live compile step with its own failure mode), this
stage reads ``infra/blueprints/<blueprintId>/main.json``, compiled ahead of time the same way
Azure Verified Modules itself ships a ``main.json`` alongside every module's ``main.bicep``
(confirmed against the AVM Bicep Resource Module Specification's required module structure).
Recompile it (``az bicep build --file main.bicep --outfile main.json``) whenever ``main.bicep``
changes; nothing here re-derives it.

Auth: the standard ARM resource scope ``https://management.azure.com/.default`` against the
deployment's own workload identity credential — secretless, same discipline as every other stage.

**Verified 2026-08-01** against the live ARM REST API surface — Microsoft Learn's own
moniker-versioned pages proved unreliable for this (three different ``?view=`` query values on the
same URL returned byte-identical ``2022-08-01-preview`` content; see
``.apm/known-pitfalls.md``), so this was instead verified against the versioned OpenAPI source and
its own worked examples in ``Azure/azure-rest-api-specs``
(``specification/resources/resource-manager/Microsoft.Resources/deploymentStacks/stable/
2025-07-01/``), cross-checked against a live subscription's actually-supported versions
(``az provider show --namespace Microsoft.Resources``, which returned ``2025-07-01``,
``2024-03-01``, and ``2022-08-01-preview`` as the three registered versions):

- ``PUT .../providers/Microsoft.Resources/deploymentStacks/{name}?api-version=2025-07-01`` creates
  or updates a stack. It is a real ARM long-running operation (``x-ms-long-running-operation: true``
  in the spec); the resource's own ``properties.provisioningState`` is authoritative and is what
  this stage polls via a plain ``GET`` on the same URL, rather than following the
  ``Azure-AsyncOperation`` header — simpler, and every SDK-less REST caller in this codebase already
  polls the resource itself (see ``devops_project.py``'s operation polling for the same reasoning
  applied to a different API).
- ``properties.actionOnUnmanage`` and ``properties.denySettings`` are both required. This stage uses
  ``detach`` (not ``delete``) for every ``actionOnUnmanage`` field — a resource leaving the template
  on a future blueprint version must never be silently deleted from a customer's tenant without a
  human-reviewable step, matching this codebase's halt-and-preserve posture everywhere else — and
  ``denyDelete`` for ``denySettings.mode``, which blocks accidental deletion of managed resources by
  any other principal in the tenant while still allowing normal reads and modifications.
- ``properties.provisioningState`` terminal values are ``succeeded``, ``failed``, ``canceled``;
  every other documented value (``creating``, ``validating``, ``waiting``, ``deploying``,
  ``canceling``, ``updatingDenyAssignments``, ``deletingResources``, ``deleting``) is in-progress.

**Idempotence (FR-029):** a ``GET`` before any write. A stack already ``succeeded`` is converged —
``SKIPPED_CONVERGED``/``NO_OP``, no write issued. Anything else (absent, ``failed``, ``canceled``,
or stuck mid-flight from an interrupted prior attempt) submits the same deterministic ``PUT`` and
polls it to completion — forward-fix falls out of ARM's own create-or-update semantics for free,
the same guarantee Azure Verified Modules and every other ARM-native resource already relies on.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

import httpx

from groundwork_contracts.audit import IdempotenceOutcome
from groundwork_contracts.deployment import Deployment
from groundwork_contracts.plan import DeploymentPlan
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.pipeline_execution import (
    PipelineRunOutcome,
    read_run_output_marker,
    stage_outcome_for,
    trigger_pipeline_run,
    wait_for_pipeline_run,
)

ARM_SCOPE = "https://management.azure.com/.default"
ARM_ENDPOINT = "https://management.azure.com"
API_VERSION = "2025-07-01"
_STACK_OUTPUTS_MARKER = "GROUNDWORK_STACK_OUTPUTS="

# Owned here, not devops_project.py, specifically so devops_pipelines.py/devops_environments.py
# (T076a/T076b — invoked from devops_project.py's own execute()) can import naming helpers from
# this module without creating an import cycle back through devops_project.py, which itself now
# imports those two modules. devops_project.py imports this constant from here.
#
# GROUNDWORK_BLUEPRINTS_PATH (set by both Dockerfiles to /app/blueprints) is authoritative in any
# deployed environment. The parents[3]-relative fallback only resolves correctly when this file
# still lives at its source-tree location relative to infra/blueprints — true for local dev and
# tests, false inside the container image, where src/ is copied to /app/src but blueprints/ is
# copied to /app/blueprints, not /app/infra/blueprints. That mismatch silently pointed every
# what-if capture and infrastructure-stage read at a nonexistent path in every deployed environment.
PLATFORM_SOURCE_ROOT = Path(
    os.environ.get("GROUNDWORK_BLUEPRINTS_PATH")
    or Path(__file__).resolve().parents[3] / "infra" / "blueprints"
)


class InfrastructureStageError(Exception):
    """A domain-level failure this stage recognised by name — the deployment stack itself
    reported ``failed``/``canceled``, or its resources never appeared."""


def template_and_parameters(
    plan: DeploymentPlan,
) -> tuple[dict[str, Any], dict[str, Any], str, dict[str, str]]:
    """The compiled ARM template, its parameters, the target location, and the tags applied —
    the exact inputs the deployment stack's own apply (:meth:`InfrastructureStage._stack_body`)
    and the what-if preview (``engine/preview.py``, T074) must agree on byte-for-byte. Extracted
    here, the one place both read from, so the two can never independently drift on what a
    customer is shown beforehand versus what is actually applied.
    """
    template_path = PLATFORM_SOURCE_ROOT / plan.blueprint_id / "main.json"
    template = json.loads(template_path.read_text(encoding="utf-8"))
    location = plan.region.value
    tags = {"managed-by": "groundwork"}
    parameters = {
        "location": {"value": location},
        "resourceGroupName": {"value": deployment_resource_group_name(plan.subscription_id)},
        "tags": {"value": tags},
        "resourceToken": {"value": resource_token(plan.subscription_id)},
    }
    return template, parameters, location, tags


def deployment_resource_group_name(subscription_id: str) -> str:
    """Duplicated, not imported, from ``groundwork_shared.validation.checks.naming`` —
    the orchestrator must never import the control plane (``test_import_boundaries.py``). Must be
    kept byte-identical to that function's own convention (``rg-groundwork-{subscriptionId[:8]}``)
    or the readiness check and this deployment silently disagree about what resource group is in
    play — the same risk ``main.bicep``'s own docstring names for ``resourceGroupName``.
    """
    return f"rg-groundwork-{subscription_id[:8]}"


def deployment_stack_name(subscription_id: str) -> str:
    return f"stack-groundwork-{subscription_id[:8]}"


def resource_token(subscription_id: str) -> str:
    """Deterministic suffix for globally-unique resource names (Key Vault, workspace).

    Must be stable across repeated deployments — the deployment stack is a create-or-update
    resource, and a token that changed between calls would make every retry create new,
    differently-named resources instead of converging on the same ones. Twelve lowercase hex
    characters keeps the tightest consumer (``kv-gw-{token}``, Key Vault's 24-character name limit)
    well inside its budget.
    """
    return hashlib.sha256(subscription_id.encode("utf-8")).hexdigest()[:12]


class InfrastructureStage:
    """Implements ``Stage`` for the blueprint's ``infrastructure`` stage.

    **Rewritten 2026-08-24 (Clarifications, FR-038a) — no longer a direct ARM deployment-stack
    call.** Triggers a run of the customer's own Azure DevOps pipeline
    (``infra/blueprints/standard-production-fabric/azure-pipelines.yml``, already pushed into the
    customer's repository by ``devops_project.py``) with ``applyChanges: true``, then polls it to
    completion — System's own credential never touches this subscription past the one bootstrap
    write (FR-006a). The pipeline's own ``AzureCLI@2`` task runs the identical ``az stack sub
    create`` this stage used to run directly, using the same deterministic
    ``resourceGroupName``/``resourceToken`` values, so a customer running that pipeline themselves
    converges on the same resources this stage's automated trigger does. See
    ``stages/pipeline_execution.py`` for the shared trigger-and-poll mechanics this reuses, and
    the research notes § V-009 for the ``[VERIFIED]`` Azure DevOps Pipelines REST facts behind it.
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
        subscription_id = context.plan.subscription_id
        organization_url = context.devops_organization_url
        if not organization_url:
            raise InfrastructureStageError(
                "no Azure DevOps organization URL for this deployment: neither the tenant "
                "record nor the worker-wide GROUNDWORK_DEVOPS_ORGANIZATION_URL setting "
                "provides one"
            )

        checkpoint = context.deployment.checkpoint
        if checkpoint is not None and checkpoint.stage_name == "infrastructure":
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
                    resources_affected=(deployment_stack_name(subscription_id),),
                    idempotence_outcome=IdempotenceOutcome.NO_OP,
                )

        template_parameters = {
            "applyChanges": True,
            "location": context.plan.region.value,
            "resourceGroupName": deployment_resource_group_name(subscription_id),
            "resourceToken": resource_token(subscription_id),
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
        if outcome.result == "succeeded":
            outputs_text = await read_run_output_marker(
                credential=context.credential,
                organization_url=organization_url,
                subscription_id=subscription_id,
                run_id=outcome.run_id,
                http_client=self._client,
                marker=_STACK_OUTPUTS_MARKER,
            )
            if outputs_text is None:
                raise InfrastructureStageError(
                    f"pipeline run {outcome.run_id} succeeded but did not emit deployment-stack "
                    f"outputs with marker {_STACK_OUTPUTS_MARKER!r}"
                )
            outputs_payload = json.loads(outputs_text)
            if not isinstance(outputs_payload, dict):
                raise InfrastructureStageError(
                    f"pipeline run {outcome.run_id} emitted non-object deployment-stack outputs"
                )
            updated_deployment = Deployment.model_validate(
                context.deployment.model_copy(
                    update={
                        "infrastructure_outputs": json.dumps(
                            outputs_payload, sort_keys=True, separators=(",", ":")
                        )
                    }
                ).model_dump()
            )
        else:
            updated_deployment = None
        return stage_outcome_for(
            outcome,
            resources_affected=(deployment_stack_name(subscription_id),),
            updated_deployment=updated_deployment,
        )

    async def _wait_for_terminal(
        self,
        credential: Any,
        organization_url: str,
        subscription_id: str,
        outcome: PipelineRunOutcome,
    ) -> PipelineRunOutcome:
        try:
            return await wait_for_pipeline_run(
                credential=credential,
                organization_url=organization_url,
                subscription_id=subscription_id,
                outcome=outcome,
                http_client=self._client,
                max_poll_attempts=self._max_poll_attempts,
                poll_interval_seconds=self._poll_interval_seconds,
            )
        except Exception as exc:
            raise InfrastructureStageError(str(exc)) from exc
