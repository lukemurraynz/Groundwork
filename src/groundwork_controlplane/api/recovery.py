"""``POST /deployments/{deploymentId}/recovery`` (T085; FR-031, FR-031a).

Chooses an outcome for a ``HALTED`` deployment. Never trusts anything the caller asserts about
*why* the deployment halted or *what* it is allowed to choose — both are re-derived from durable
state via ``engine/halt.py``'s ``reconstruct_halted_view``, the same reconstruction
``GET /deployments/{deploymentId}`` uses, so a customer cannot request an action a stage's own
declared recovery path never offered.

**Retry and forward-fix are both real, working requeues** — the API contract's own words ("resume
from the last checkpoint without re-running completed stages", FR-030) describe exactly what
``engine/halt.py``'s ``requeue_after_recovery_choice`` does, identically to how an *automatic*
transient-failure retry already resumes a deployment. The two actions are mechanically
indistinguishable here; what differs is only the human's prior, out-of-band reasoning for choosing
one over the other (fixed a quota, vs. simply trying again), which this system has no way to observe
and does not need to.

**Rollback's approval gate and its execution are both real (ADR-0009, 2026-08-26).** No ``Stage``
in this codebase has a teardown/delete counterpart to its ``execute`` — direct ARM teardown was
ruled out (FR-006a restricts Groundwork's own credential to a single bootstrap write, and
``stages/infrastructure.py`` sets ``denySettings.mode: denyDelete`` on its own Deployment Stack to
protect managed resources from deletion by any principal in the tenant, Groundwork included).
Rollback instead **redeploys the last known-good configuration** — the parameter set captured from
the deployment's own succeeded ``infrastructure`` run — through a ``rollback.yml`` pipeline inside
the *customer's own* Azure DevOps project, the identical trigger-and-poll execution model every
other stage already uses. Because it redeploys prior state rather than deleting, it never fights
``denyDelete``, and Groundwork's own credential never touches the customer's subscription. Every
check before execution (action offered, approval present, approval real and complete, approval
bound to the same plan being rolled back, approval distinct from the one that authorised the
original execution, captured infrastructure outputs present) is enforced exactly as it always was;
what changed with ADR-0009 is that a fully validated and approved rollback request now actually
queues and runs the pipeline instead of answering ``501``.

**A cost-reapproval retry (T075a, FR-019) is gated exactly like rollback — a fresh, distinct
approval required.** `engine/cost_preflight.py`'s own module docstring explains why a bare retry
is not enough here: unlike every other halt reason, a cost overrun can never be resolved without
an actual new human decision. The `StageError.code` recorded at halt time
(`CostReapprovalRequired` vs. `CostReapprovalEscalationRequired`) is what tells this endpoint
whether the fresh approval must itself carry a `second_approval` — a single self-approval cannot
satisfy an *escalated* re-approval, the same SC-018 zero-tolerance the original approval flow
already enforces, now applied to this path too. On success, the deployment's own `authority` is
updated to the new approval before requeuing — the audit trail must reflect which approval actually
authorised the resumed execution, not the now-superseded one whose cost figure is what triggered
this halt in the first place.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Annotated, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from groundwork_contracts.approval import PendingApproval
from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_controlplane.api.auth import AuthenticatedCaller
from groundwork_controlplane.api.deployments import (
    build_deployment_response,
    resolve_deployment_blueprint,
)
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_controlplane.approval.lookup import find_approval_by_id
from groundwork_orchestrator.engine.cost_preflight import COST_REAPPROVAL_PSEUDO_STAGE
from groundwork_orchestrator.engine.halt import (
    load_all_stage_records,
    reconstruct_halted_view,
    requeue_after_recovery_choice,
)
from groundwork_orchestrator.stages.pipeline_execution import (
    PipelineExecutionError,
    deployment_project_name,
    rollback_pipeline_name,
    stage_outcome_for,
    trigger_pipeline_run,
    wait_for_pipeline_run,
)
from groundwork_shared.telemetry.scrubbing import scrub_text

router = APIRouter(prefix="/v1", tags=["deployments"])

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _now(request: Request) -> datetime:
    now_fn = getattr(request.app.state, "now_fn", None)
    if callable(now_fn):
        current = now_fn()
        if isinstance(current, datetime):
            return current
    return datetime.now(UTC)


async def _organization_url_for_deployment(request: Request, deployment: Deployment) -> str | None:
    tenant_repository = getattr(request.app.state, "tenant_repository", None)
    settings = getattr(request.app.state, "settings", None)
    fallback = getattr(getattr(settings, "readiness", None), "devops_organization_url", None)
    if tenant_repository is None:
        return fallback
    tenant = await tenant_repository.read(deployment.tenant_id, deployment.tenant_id)
    if tenant is None:
        return fallback
    return tenant.devops_organization_url or fallback


def _rolled_back_deployment(request: Request, deployment: Deployment) -> Deployment:
    updated = deployment.model_copy(
        update={
            "status": DeploymentStatus.ROLLED_BACK,
            "current_stage": None,
            "completed_at": _now(request),
        }
    )
    return Deployment.model_validate(updated.model_dump())


class RecoveryRequest(BaseModel):
    """``{ action, approvalId? }`` — exactly what ``control-plane-api.md`` documents."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    action: Literal["retry", "forward_fix", "rollback"]
    approval_id: Annotated[str | None, Field(alias="approvalId", pattern=_GUID)] = None


@router.post("/deployments/{deployment_id}/recovery", status_code=202)
async def choose_recovery(
    deployment_id: str,
    body: RecoveryRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    deployment_repository = request.app.state.deployment_repository
    stage_record_repository = request.app.state.stage_record_repository

    deployment = await deployment_repository.read(caller.tenant_id, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    blueprint = await resolve_deployment_blueprint(request, deployment)
    if deployment.status is not DeploymentStatus.HALTED:
        raise HTTPException(
            status_code=409,
            detail=(
                f"deployment is {deployment.status.value!r}, not 'halted'; there is nothing to "
                f"recover"
            ),
        )

    stage_records = await load_all_stage_records(
        stage_record_repository, caller.tenant_id, deployment_id
    )
    halted_view = reconstruct_halted_view(stage_records, blueprint)
    if halted_view is None:
        # Unreachable in practice: a HALTED deployment always has a FAILED stage record behind it
        # (engine/halt.py's own module docstring invariant). Surfaced as a clear 500 rather than a
        # confusing "action not offered" if that invariant is ever violated.
        raise HTTPException(
            status_code=500, detail="halted deployment has no reconstructable failure"
        )

    if body.action not in halted_view.recovery_options:
        raise HTTPException(
            status_code=422,
            detail=(
                f"{body.action!r} is not an available recovery option for the "
                f"{halted_view.failing_stage!r} stage; available: "
                f"{list(halted_view.recovery_options)}"
            ),
        )

    if body.action == "rollback":
        if body.approval_id is None:
            # FR-031a: rollback is itself an irreversible action and must never be made convenient.
            raise HTTPException(
                status_code=403, detail="rollback requires its own approvalId (FR-031a)"
            )
        approval = await find_approval_by_id(
            pending_repository=request.app.state.pending_approval_repository,
            approval_repository=request.app.state.approval_repository,
            tenant_id=caller.tenant_id,
            approval_id=body.approval_id,
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="rollback approval not found")
        if isinstance(approval, PendingApproval):
            raise HTTPException(
                status_code=409,
                detail="rollback approval is incomplete; a second approver is still required",
            )
        if approval.plan_hash != deployment.authority.plan_hash:
            raise HTTPException(
                status_code=409,
                detail="rollback approval does not carry the plan hash of the deployment being "
                "rolled back",
            )
        if approval.approval_id == deployment.authority.approval_id:
            # FR-031a: rollback's own explicit approval must be distinct from the one that
            # authorised the original execution — reusing that approval_id would let a single
            # earlier approval retroactively authorise an unrelated, irreversible teardown.
            raise HTTPException(
                status_code=403,
                detail="rollback requires an approval distinct from the one that authorised "
                "execution (FR-031a)",
            )
        if deployment.infrastructure_outputs is None:
            raise HTTPException(
                status_code=409,
                detail="rollback requires captured infrastructure outputs from a previously "
                "succeeded infrastructure run",
            )
        organization_url = await _organization_url_for_deployment(request, deployment)
        if organization_url is None:
            raise HTTPException(
                status_code=409,
                detail="cannot trigger rollback: neither the tenant record nor the worker-wide "
                "settings provide an Azure DevOps organization URL",
            )

        outputs_payload = json.loads(deployment.infrastructure_outputs)
        if not isinstance(outputs_payload, dict):
            raise HTTPException(
                status_code=409,
                detail="rollback requires infrastructure outputs to be stored as a JSON object",
            )

        injected_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
        owns_client = injected_client is None
        http_client = injected_client or httpx.AsyncClient(timeout=30.0)
        rollback_pipeline = rollback_pipeline_name(
            deployment_project_name(deployment.subscription_id)
        )
        try:
            pipeline_run = await trigger_pipeline_run(
                credential=request.app.state.credential,
                organization_url=organization_url,
                subscription_id=deployment.subscription_id,
                http_client=http_client,
                template_parameters={
                    "rollbackToOutputs": json.dumps(
                        outputs_payload, sort_keys=True, separators=(",", ":")
                    )
                },
                pipeline_name_override=rollback_pipeline,
            )
            pipeline_run = await wait_for_pipeline_run(
                credential=request.app.state.credential,
                organization_url=organization_url,
                subscription_id=deployment.subscription_id,
                outcome=pipeline_run,
                http_client=http_client,
                max_poll_attempts=120,
                poll_interval_seconds=5.0,
                pipeline_name_override=rollback_pipeline,
            )
        except (PipelineExecutionError, httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail=scrub_text(str(exc))) from exc
        finally:
            if owns_client:
                await http_client.aclose()

        rollback_outcome = stage_outcome_for(pipeline_run, resources_affected=())
        if rollback_outcome.status is not rollback_outcome.status.SUCCEEDED:
            detail = (
                rollback_outcome.error.message
                if rollback_outcome.error is not None
                else "rollback pipeline failed"
            )
            raise HTTPException(status_code=502, detail=scrub_text(detail))

        saved = await deployment_repository.replace(
            caller.tenant_id, _rolled_back_deployment(request, deployment)
        )
        return await build_deployment_response(
            saved,
            blueprint=blueprint,
            stage_record_repository=stage_record_repository,
            credential=request.app.state.credential,
        )

    deployment_to_requeue = deployment
    if halted_view.failing_stage == COST_REAPPROVAL_PSEUDO_STAGE:
        # T075a/FR-019: unlike every other halt reason, this one can never be resolved by a bare
        # retry — a cost overrun requires an actual new human decision. Gated exactly like
        # rollback: a fresh, distinct approval required, escalated to a second approver if the
        # recomputed figure crossed the FR-020a threshold (recorded in the StageError.code at
        # halt time, since DeploymentStageRecord has no dedicated field for it).
        if body.approval_id is None:
            raise HTTPException(
                status_code=403,
                detail="a cost re-approval requires its own approvalId (FR-019)",
            )
        approval = await find_approval_by_id(
            pending_repository=request.app.state.pending_approval_repository,
            approval_repository=request.app.state.approval_repository,
            tenant_id=caller.tenant_id,
            approval_id=body.approval_id,
        )
        if approval is None:
            raise HTTPException(status_code=404, detail="cost re-approval not found")
        if isinstance(approval, PendingApproval):
            raise HTTPException(
                status_code=409,
                detail="cost re-approval is incomplete; a second approver is still required",
            )
        if approval.plan_hash != deployment.authority.plan_hash:
            raise HTTPException(
                status_code=409,
                detail="cost re-approval does not carry the plan hash of the deployment being "
                "resumed",
            )
        if approval.approval_id == deployment.authority.approval_id:
            raise HTTPException(
                status_code=403,
                detail="cost re-approval requires an approval distinct from the one that "
                "authorised the original execution (FR-019)",
            )
        if (
            halted_view.error.code == "CostReapprovalEscalationRequired"
            and approval.second_approval is None
        ):
            raise HTTPException(
                status_code=409,
                detail="the recomputed cost crosses the second-approver threshold; this "
                "re-approval must itself carry a second approval, not a single self-approval "
                "(FR-019, FR-020a, SC-018)",
            )
        # Real audit correctness, not cosmetic: the deployment resumes under the NEW approval,
        # not the now-superseded one whose cost figure triggered this halt.
        deployment_to_requeue = deployment.model_copy(
            update={
                "authority": AuthorityChain(
                    plan_hash=approval.plan_hash, approval_id=approval.approval_id
                )
            }
        )

    updated = requeue_after_recovery_choice(deployment_to_requeue)
    saved = await deployment_repository.replace(caller.tenant_id, updated)
    return await build_deployment_response(
        saved,
        blueprint=blueprint,
        stage_record_repository=stage_record_repository,
        credential=request.app.state.credential,
    )
