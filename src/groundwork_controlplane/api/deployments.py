"""``POST /deployments`` and the status monitor (T084).

Every deployment is created ``status: "queued"`` — that is this route's own creation-time state,
never anything past it. What happens afterwards (transition to ``executing``, per-stage progress,
``halted``/``succeeded``) is the queue-consumption loop's doing (``engine/queue_loop.py``, wired
into ``groundwork_orchestrator.worker``'s lifespan), not this module's. ``GET
/deployments/{deploymentId}`` reflects that real, independently-running state on every call — it
reconstructs ``stages`` and ``halted`` fresh from ``DeploymentStageRecord`` history via
``engine/halt.py`` (T073), never a cached or narrated copy of it.

The per-subscription lease (``groundwork_shared.queue.subscription_lease``, T069 — moved out of
this package 2026-08-01 since the orchestrator's queue-consumption loop needs to import it too and
must never import ``groundwork_controlplane``) is deliberately **not** acquired here. Acquiring a
real exclusivity lease for work nothing has started yet would hold it for up to its full TTL for no
purpose — that belongs to the sequencer's own caller, which actually does something with it once
acquired.
"""

from __future__ import annotations

import re
import uuid
from typing import Annotated

from azure.core.credentials_async import AsyncTokenCredential
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from groundwork_contracts.approval import Approval, PendingApproval
from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import SealedDeploymentPlan
from groundwork_controlplane.api.auth import AuthenticatedCaller
from groundwork_controlplane.api.plans import get_authenticated_caller, get_customer_tenant
from groundwork_controlplane.approval.lookup import find_approval_by_id
from groundwork_controlplane.queue.admission import evaluate_admission
from groundwork_orchestrator.engine.halt import (
    build_stage_status_view,
    load_all_stage_records,
    reconstruct_halted_view,
    recovery_action_help,
)
from groundwork_orchestrator.state.repositories import DeploymentRepository, StageRecordRepository
from groundwork_shared.storage.sas import read_only_sas_url
from groundwork_shared.telemetry.correlation import current_correlation_id

router = APIRouter(prefix="/v1", tags=["deployments"])

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class CreateDeploymentRequest(BaseModel):
    """``{ approvalId, planHash }`` — exactly what ``control-plane-api.md`` documents."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    approval_id: Annotated[str, Field(alias="approvalId", pattern=_GUID)]
    plan_hash: Annotated[str, Field(alias="planHash", pattern=r"^sha256:[0-9a-f]{64}$")]


async def resolve_deployment_blueprint(
    request: Request, deployment: Deployment
) -> PlatformBlueprint:
    """Resolve the deployment's blueprint from its approved plan hash."""
    plan_repository = getattr(request.app.state, "plan_repository", None)
    if plan_repository is None:
        fallback = getattr(request.app.state, "blueprint", None)
        if isinstance(fallback, PlatformBlueprint):
            return fallback
        raise HTTPException(status_code=500, detail="plan repository is not configured")
    sealed: SealedDeploymentPlan | None = await plan_repository.read(
        deployment.tenant_id, deployment.authority.plan_hash
    )
    if sealed is None:
        fallback = getattr(request.app.state, "blueprint", None)
        if isinstance(fallback, PlatformBlueprint):
            return fallback
        raise HTTPException(
            status_code=500,
            detail=("deployment references a plan hash that is not present in the plan repository"),
        )
    catalogue: dict[str, PlatformBlueprint] = getattr(request.app.state, "blueprints", {})
    blueprint = catalogue.get(sealed.plan.blueprint_id)
    if blueprint is None:
        raise HTTPException(
            status_code=500,
            detail=f"approved blueprint {sealed.plan.blueprint_id!r} is not loaded",
        )
    return blueprint


async def build_deployment_response(
    deployment: Deployment,
    *,
    blueprint: PlatformBlueprint,
    stage_record_repository: StageRecordRepository,
    credential: AsyncTokenCredential,
) -> dict[str, object]:
    """The status-monitor response shape (``control-plane-api.md``'s ``GET /deployments`` example).

    Exported (not module-private) so ``api/recovery.py`` (T085) can return the same shape after
    resolving a customer's recovery choice, rather than the caller having to re-``GET`` immediately
    afterwards to see the result. Reads ``DeploymentStageRecord`` history fresh on every call —
    there is no cached or denormalised copy of it on ``Deployment`` itself (see
    ``engine/halt.py``'s own module docstring for why that is a deliberate choice, not a gap).
    """
    stage_records = await load_all_stage_records(
        stage_record_repository, deployment.tenant_id, deployment.deployment_id
    )
    stages = build_stage_status_view(
        stage_records,
        execution_order=blueprint.execution_order(),
        current_stage=deployment.current_stage,
    )
    halted_payload: dict[str, object] | None = None
    if deployment.status is DeploymentStatus.HALTED:
        halted_view = reconstruct_halted_view(stage_records, blueprint)
        if halted_view is not None:
            halted_payload = {
                "failingStage": halted_view.failing_stage,
                "error": {
                    "code": halted_view.error.code,
                    "message": halted_view.error.message,
                    "isTransient": halted_view.error.is_transient,
                },
                "recoveryOptions": list(halted_view.recovery_options),
                "recoveryOptionsHelp": {
                    action: recovery_action_help(action) for action in halted_view.recovery_options
                },
            }
    return {
        "deploymentId": deployment.deployment_id,
        "status": deployment.status.value,
        "queuePosition": deployment.queue_position,
        "currentStage": deployment.current_stage,
        "correlationId": deployment.correlation_id,
        "stages": [
            {
                "stageName": s.stage_name,
                "status": s.status,
                "idempotenceOutcome": s.idempotence_outcome,
                "attempt": s.attempt,
            }
            for s in stages
        ],
        "whatIfArtefactUri": (
            await read_only_sas_url(deployment.what_if_artefact_uri, credential=credential)
            if deployment.what_if_artefact_uri is not None
            else None
        ),
        "halted": halted_payload,
    }


async def queue_deployment_for_approval(
    request: Request,
    caller: AuthenticatedCaller,
    approval: Approval,
    *,
    deployment_id: str | None = None,
) -> Deployment:
    """Admit and queue a deployment for a completed, real :class:`Approval`.

    Exported (not module-private) so the voice channel's ``POST /v1/voice/approve``
    (``api/voice.py``) can queue a deployment through exactly this path — the same tenant
    admission check, the same subscription/plan-hash binding taken from the approval record
    itself (never from caller-supplied request fields), the same audit-relevant
    :class:`AuthorityChain`. The deterministic-execution boundary requires orchestration to have
    one real entry point, not a second, looser one a different channel invented independently.
    """
    deployment_repository: DeploymentRepository = request.app.state.deployment_repository
    tenant = await get_customer_tenant(request, caller.tenant_id)

    already_used = await _find_deployment_for_approval(
        deployment_repository, tenant_id=caller.tenant_id, approval_id=approval.approval_id
    )
    if already_used is not None:
        raise HTTPException(
            status_code=409, detail="this approval already has a deployment queued or executed"
        )

    queue_position = await evaluate_admission(
        deployment_repository, caller.tenant_id, concurrency_cap=tenant.concurrency_cap
    )

    deployment = Deployment(
        deployment_id=deployment_id or str(uuid.uuid4()),
        tenant_id=caller.tenant_id,
        subscription_id=approval.approved_parameters.subscription_id,
        correlation_id=current_correlation_id() or str(uuid.uuid4()),
        authority=AuthorityChain(plan_hash=approval.plan_hash, approval_id=approval.approval_id),
        status=DeploymentStatus.QUEUED,
        queue_position=queue_position,
    )
    return await deployment_repository.create(caller.tenant_id, deployment)


@router.post("/deployments", status_code=202)
async def create_deployment(
    body: CreateDeploymentRequest,
    request: Request,
    response: Response,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    repeatability_request_id: Annotated[
        str | None, Header(alias="Repeatability-Request-ID")
    ] = None,
) -> dict[str, object]:
    deployment_repository = request.app.state.deployment_repository
    stage_record_repository = request.app.state.stage_record_repository

    # Idempotency: a caller-supplied Repeatability-Request-ID becomes the deployment's own id, so
    # a repeat submission naturally lands on the same Cosmos document instead of needing a
    # separate dedupe table. An invalid (non-GUID) value is a client error, not silently ignored.
    if repeatability_request_id is not None:
        if not re.match(_GUID, repeatability_request_id):
            raise HTTPException(status_code=400, detail="Repeatability-Request-ID must be a GUID")
        deployment_id = repeatability_request_id
        existing = await deployment_repository.read(caller.tenant_id, deployment_id)
        if existing is not None:
            blueprint = await resolve_deployment_blueprint(request, existing)
            response.headers["Operation-Location"] = f"/v1/deployments/{existing.deployment_id}"
            return await build_deployment_response(
                existing,
                blueprint=blueprint,
                stage_record_repository=stage_record_repository,
                credential=request.app.state.credential,
            )
    else:
        deployment_id = str(uuid.uuid4())

    approval = await find_approval_by_id(
        pending_repository=request.app.state.pending_approval_repository,
        approval_repository=request.app.state.approval_repository,
        tenant_id=caller.tenant_id,
        approval_id=body.approval_id,
    )
    if approval is None:
        raise HTTPException(status_code=404, detail="approval not found")
    if isinstance(approval, PendingApproval):
        raise HTTPException(
            status_code=409, detail="approval is incomplete; a second approver is still required"
        )
    if approval.plan_hash != body.plan_hash:
        raise HTTPException(
            status_code=409,
            detail="planHash does not match the approval; the plan was superseded",
        )
    # An above-threshold Approval that is self-approved cannot exist — Approval's own
    # `_second_approver_present_and_distinct` validator refuses to construct one. "409 if
    # self-approved above threshold" (control-plane-api.md) is therefore unreachable here by
    # construction, not by a check this route has to perform.

    created = await queue_deployment_for_approval(
        request, caller, approval, deployment_id=deployment_id
    )

    response.headers["Operation-Location"] = f"/v1/deployments/{created.deployment_id}"
    response.headers["Retry-After"] = "30"
    blueprint = await resolve_deployment_blueprint(request, created)
    return await build_deployment_response(
        created,
        blueprint=blueprint,
        stage_record_repository=stage_record_repository,
        credential=request.app.state.credential,
    )


@router.get("/deployments/{deployment_id}")
async def get_deployment(
    deployment_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    deployment_repository = request.app.state.deployment_repository
    deployment = await deployment_repository.read(caller.tenant_id, deployment_id)
    if deployment is None:
        raise HTTPException(status_code=404, detail="deployment not found")
    blueprint = await resolve_deployment_blueprint(request, deployment)
    return await build_deployment_response(
        deployment,
        blueprint=blueprint,
        stage_record_repository=request.app.state.stage_record_repository,
        credential=request.app.state.credential,
    )


async def _find_deployment_for_approval(
    deployment_repository: DeploymentRepository, *, tenant_id: str, approval_id: str
) -> Deployment | None:
    async for deployment in deployment_repository.query(
        tenant_id,
        "SELECT * FROM c WHERE c.authority.approval_id = @approval_id",
        [{"name": "@approval_id", "value": approval_id}],
    ):
        return deployment
    return None
