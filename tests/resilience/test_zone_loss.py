"""T061 / SC-021 / FR-041b — simulated zone loss pauses then resumes through orphan recovery."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from tests.conftest import TENANT_ID
from tests.unit.test_queue_consumption import (
    DEPLOYMENT_ID,
    NOW,
    _FakeCredentialFactory,
    _FakeSequencer,
    _Harness,
    _poll,
    _queued_deployment,
    _sealed_plan,
    _succeeded_run_result,
)

from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_orchestrator.engine.queue_loop import DeploymentAttemptResult

pytestmark = pytest.mark.resilience


async def _poll_and_persist(harness: _Harness, sequencer: _FakeSequencer) -> list[Any]:
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())
    for outcome in outcomes:
        if outcome.run_result is not None:
            await harness.deployment_repository.replace(
                outcome.tenant_id, outcome.run_result.deployment
            )
    return outcomes


async def test_zone_loss_requeues_orphaned_execution_then_resumes_without_rollback(
    valid_plan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    orphaned = Deployment.model_validate(
        _queued_deployment(plan_hash=plan_hash)
        .model_copy(
            update={
                "status": DeploymentStatus.EXECUTING,
                "queue_position": None,
                "current_stage": "infrastructure",
                "started_at": NOW,
                "lease": SubscriptionLease(
                    holder=DEPLOYMENT_ID, expires_at=NOW - timedelta(minutes=1)
                ),
            }
        )
        .model_dump()
    )
    await harness.seed_deployment(orphaned)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    first_cycle = await _poll_and_persist(harness, _FakeSequencer(_succeeded_run_result))
    assert [outcome.result for outcome in first_cycle] == [DeploymentAttemptResult.RECOVERED]

    paused = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert paused.status is DeploymentStatus.QUEUED
    assert paused.lease is None
    assert paused.current_stage is None
    assert Deployment.model_validate(paused.model_dump()) == paused

    second_cycle = await _poll_and_persist(harness, _FakeSequencer(_succeeded_run_result))
    assert [outcome.result for outcome in second_cycle] == [DeploymentAttemptResult.EXECUTED]
    assert second_cycle[0].run_result is not None
    assert second_cycle[0].run_result.deployment.status is DeploymentStatus.SUCCEEDED

    resumed = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert resumed.status is DeploymentStatus.SUCCEEDED
