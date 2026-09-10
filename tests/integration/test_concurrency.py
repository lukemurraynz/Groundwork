"""T064 / SC-022 / FR-045a-b-c — queueing, serialisation, unrelated-tenant progress."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from tests.conftest import APPROVAL_ID, CORRELATION_ID, OTHER_TENANT_ID, SUBSCRIPTION_ID, TENANT_ID
from tests.unit.test_queue_consumption import (
    DEPLOYMENT_ID,
    NOW,
    OTHER_DEPLOYMENT_ID,
    OTHER_SUBSCRIPTION_ID,
    _FakeCredentialFactory,
    _FakeSequencer,
    _Harness,
    _poll,
    _queued_deployment,
    _sealed_plan,
    _succeeded_run_result,
)
from tests.unit.test_subscription_lease import _FakeLeaseContainer

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_controlplane.queue.admission import compute_admission
from groundwork_orchestrator.engine.queue_loop import DeploymentAttemptResult
from groundwork_shared.queue.subscription_lease import LeaseHeldError, SubscriptionLeaseStore

pytestmark = pytest.mark.integration


def test_compute_admission_reports_queue_position_beyond_cap() -> None:
    assert compute_admission(active_count=3, queued_count=0, concurrency_cap=3) == 1
    assert compute_admission(active_count=5, queued_count=2, concurrency_cap=3) == 3


async def test_subscription_lease_serialises_same_subscription() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())
    first = await store.acquire(SUBSCRIPTION_ID, holder="dep-a", now=NOW)
    assert first.holder == "dep-a"

    with pytest.raises(LeaseHeldError):
        await store.acquire(SUBSCRIPTION_ID, holder="dep-b", now=NOW + timedelta(seconds=1))


async def test_unrelated_tenant_progresses_while_other_tenant_is_lease_blocked(valid_plan) -> None:
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    blocked = _queued_deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    unrelated = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(blocked)
    await harness.seed_deployment(unrelated)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))
    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="already-running", now=NOW)

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    by_id = {outcome.deployment_id: outcome.result for outcome in outcomes}
    assert by_id[DEPLOYMENT_ID] is DeploymentAttemptResult.LEASE_HELD
    assert by_id[OTHER_DEPLOYMENT_ID] is DeploymentAttemptResult.EXECUTED
    assert [call[0].deployment_id for call in sequencer.calls] == [OTHER_DEPLOYMENT_ID]


async def test_per_tenant_cap_queue_positions_are_preserved_through_creation_shape() -> None:
    active_executing = Deployment(
        deployment_id="11111111-1111-1111-1111-111111111112",
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=f"sha256:{'a' * 64}", approval_id=APPROVAL_ID),
        status=DeploymentStatus.EXECUTING,
        started_at=datetime(2026, 8, 1, tzinfo=UTC),
        lease=SubscriptionLease(
            holder="run-1", expires_at=datetime(2026, 8, 1, tzinfo=UTC) + timedelta(hours=1)
        ),
    )
    queued_one = _queued_deployment(
        deployment_id="11111111-1111-1111-1111-111111111113",
        tenant_id=TENANT_ID,
        subscription_id="44444444-4444-4444-4444-444444444444",
        plan_hash=f"sha256:{'a' * 64}",
        queue_position=1,
    )
    queue_position = compute_admission(active_count=2, queued_count=1, concurrency_cap=2)
    incoming = _queued_deployment(
        deployment_id="11111111-1111-1111-1111-111111111114",
        tenant_id=TENANT_ID,
        subscription_id="55555555-5555-5555-5555-555555555555",
        plan_hash=f"sha256:{'a' * 64}",
        queue_position=queue_position,
    )

    assert active_executing.status is DeploymentStatus.EXECUTING
    assert queued_one.queue_position == 1
    assert incoming.queue_position == 2
