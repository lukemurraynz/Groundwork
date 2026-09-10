"""Durable checkpointing (T071; FR-030, FR-035).

FR-035's resume invariant: a replica taking over an execution must be able to resume from the
checkpoint alone. Nothing here holds resume state anywhere but Cosmos — there is no in-process
cache a takeover could miss, because there is no code path that writes one.

Deliberately thin. :class:`~groundwork_contracts.deployment.Checkpoint` already carries the
invariant that matters (which stage, when); this module is only the persistence step —
constructing the updated, still-frozen :class:`~groundwork_contracts.deployment.Deployment` and
writing it via :meth:`~groundwork_orchestrator.state.cosmos.TenantScopedRepository.replace`.
"""

from __future__ import annotations

from datetime import datetime

from groundwork_contracts.deployment import Checkpoint, Deployment
from groundwork_orchestrator.state.repositories import DeploymentRepository


async def record_checkpoint(
    deployment_repository: DeploymentRepository,
    deployment: Deployment,
    *,
    completed_stage: str,
    resume_token: str,
    now: datetime,
) -> Deployment:
    """Persist that ``completed_stage`` finished, so a takeover resumes after it, not from it.

    Args:
        completed_stage: The stage that just finished. On resume, the sequencer skips every
            stage up to and including this one and continues with whatever comes next in
            ``PlatformBlueprint.execution_order()``.
        resume_token: Opaque to this module — whatever the stage itself needs to confirm its own
            idempotence contract if re-examined, not interpreted here.
    """
    checkpoint = Checkpoint(stage_name=completed_stage, resume_token=resume_token, recorded_at=now)
    updated = deployment.model_copy(update={"checkpoint": checkpoint, "current_stage": None})
    return await deployment_repository.replace(deployment.tenant_id, updated)
