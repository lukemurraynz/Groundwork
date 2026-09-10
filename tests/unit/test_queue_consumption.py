"""The deployment-queue consumption loop (``engine/queue_loop.py``) — the decision logic only.

Every dependency is faked at the same boundary the rest of this codebase already fakes at: a
Cosmos ``ContainerLike``/``LeaseContainerLike`` implementation with real optimistic-concurrency
semantics for the lease container (copied from ``test_subscription_lease.py``'s own fake, since the
race behaviour it protects is exactly what a naive dict would paper over), wrapped in the real
``TenantScopedRepository``/``SubscriptionLeaseStore`` classes this module actually uses. The
sequencer itself is faked directly (a bare ``run()`` recorder) rather than assembled from a real
blueprint and real stages — ``test_sequencer.py`` already covers the sequencer's own behaviour in
full; what this file needs to prove is only that the loop calls it correctly and reacts correctly to
whatever it returns or raises.

Every deployment in this file references the same ``valid_plan`` fixture's own content hash as its
``authority.plan_hash`` — ``SealedDeploymentPlan`` requires ``plan_hash == plan.content_hash()``
exactly, so fabricating a second, different-looking hash for a "second plan" would either fail that
validator or silently desync the deployment from the plan it is supposed to reference. Two tenants
each holding a plan with the *same* content hash, in their own partitions, is realistic anyway —
content hashes are not tenant-scoped identity, storage partitioning is.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from groundwork_contracts.audit import AuthorityChain, StageError
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan, ValidityWindow
from groundwork_contracts.tenant import ConsentState, CustomerTenant
from groundwork_orchestrator.engine.halt import requeue_after_recovery_choice
from groundwork_orchestrator.engine.queue_loop import DeploymentAttemptResult, poll_once
from groundwork_orchestrator.engine.sequencer import HaltedState, RunResult
from groundwork_orchestrator.state.cosmos import TenantRegistry, TenantScopedRepository
from groundwork_orchestrator.state.repositories import CustomerTenantRepository
from groundwork_shared.queue.subscription_lease import SubscriptionLeaseStore
from tests.conftest import APPROVAL_ID, CORRELATION_ID, REQUESTER_ID, TENANT_ID

OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
OTHER_SUBSCRIPTION_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
OTHER_DEPLOYMENT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)

ORG_URL = "https://dev.azure.com/customer-org"
FABRIC_UPN = "capacity-admin@customer.example"


# ---------------------------------------------------------------------------
# Fakes — Cosmos containers with real create/read/query semantics, no network.
# ---------------------------------------------------------------------------


class _FakeContainer:
    """Backs ``TenantScopedRepository``. Keyed by ``(tenantId, id)`` — a real Cosmos container only
    guarantees ``id`` uniqueness *within* a partition, so two different tenants' documents sharing
    the same ``id`` (e.g. two plans with the same content hash) must not collide here either.
    ``query_items`` filters by partition key and, since the loop's own query is always the same
    literal string, by a simple ``status == 'queued'`` check — enough fidelity for what this module
    actually issues, without reimplementing a SQL parser.
    """

    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._etag_counter = 0
        self.fail_replace_ids: set[str] = set()

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return str(self._etag_counter)

    def _key(self, doc: Mapping[str, Any]) -> tuple[str, str]:
        return (doc["tenantId"], doc["id"])

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        stored = {**body, "_etag": self._next_etag()}
        self.documents[self._key(body)] = stored
        return stored

    async def upsert_item(
        self,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        **_: Any,
    ) -> Mapping[str, Any]:
        key = self._key(body)
        existing = self.documents.get(key)
        if body["id"] in self.fail_replace_ids:
            raise RuntimeError(f"replace failed for {body['id']}")
        if (
            match_condition is MatchConditions.IfNotModified
            and existing is not None
            and existing.get("_etag") != etag
        ):
            raise CosmosAccessConditionFailedError(status_code=412, message="etag mismatch")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[key] = stored
        return stored

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        found = self.documents.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **_: Any,
    ) -> AsyncIterator[Mapping[str, Any]]:
        for doc in self.documents.values():
            if partition_key is not None and doc.get("tenantId") != partition_key:
                continue
            if "queued" in query and doc.get("status") != "queued":
                continue
            if "executing" in query and doc.get("status") != "executing":
                continue
            yield doc


class _FakeTenantRegistryContainer:
    def __init__(self, tenant_ids: list[str]) -> None:
        self._tenant_ids = tenant_ids

    def query_items(
        self, query: str, *, parameters: list[dict[str, Any]] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for tenant_id in self._tenant_ids:
                yield tenant_id

        return _gen()


class _FakeLeaseContainer:
    """Real Cosmos etag semantics, no network — copied from ``test_subscription_lease.py``'s own
    fake, since the property this module relies on (a lease-held deployment must never be able to
    also acquire it) is exactly what a simplified dict fake would paper over."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return str(self._etag_counter)

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        item_id = body["id"]
        if item_id in self.documents:
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[item_id] = stored
        return stored

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        found = self.documents.get(item)
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        **_: Any,
    ) -> Mapping[str, Any]:
        existing = self.documents.get(item)
        if existing is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        if match_condition is MatchConditions.IfNotModified and existing["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="etag mismatch")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[item] = stored
        return stored


class _FakeSequencer:
    """Records every call and either returns a scripted :class:`RunResult` or raises a scripted
    exception — this file's stand-in for the real ``Sequencer``, which ``test_sequencer.py`` already
    covers on its own terms."""

    def __init__(
        self,
        result: Callable[[Deployment], RunResult] | Exception | None = None,
    ) -> None:
        self._result = _succeeded_run_result if result is None else result
        self.calls: list[tuple[Deployment, DeploymentPlan, object]] = []
        self.engagement: list[tuple[str | None, str | None]] = []

    async def run(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        *,
        credential: object,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult:
        self.calls.append((deployment, plan, credential))
        self.engagement.append((devops_organization_url, fabric_capacity_admin_upn))
        if isinstance(self._result, Exception):
            raise self._result
        return self._result(deployment)


class _FakeScopedCredential:
    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def for_tenant(self, tenant_id: str) -> str:
        assert tenant_id == self._tenant_id
        return f"credential-for-{tenant_id}"


class _FakeCredentialFactory:
    def __init__(self) -> None:
        self.scoped_calls: list[str] = []

    def scoped_to(self, tenant_id: str) -> _FakeScopedCredential:
        self.scoped_calls.append(tenant_id)
        return _FakeScopedCredential(tenant_id)


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _queued_deployment(
    *,
    deployment_id: str = DEPLOYMENT_ID,
    tenant_id: str = TENANT_ID,
    subscription_id: str = SUBSCRIPTION_ID,
    plan_hash: str,
    queue_position: int | None = 0,
) -> Deployment:
    return Deployment(
        deployment_id=deployment_id,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=plan_hash, approval_id=APPROVAL_ID),
        status=DeploymentStatus.QUEUED,
        queue_position=queue_position,
    )


def _executing_deployment(
    *,
    deployment_id: str = DEPLOYMENT_ID,
    tenant_id: str = TENANT_ID,
    subscription_id: str = SUBSCRIPTION_ID,
    plan_hash: str,
    lease: SubscriptionLease | None,
) -> Deployment:
    return Deployment(
        deployment_id=deployment_id,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=plan_hash, approval_id=APPROVAL_ID),
        status=DeploymentStatus.EXECUTING,
        current_stage="infrastructure",
        started_at=NOW - timedelta(minutes=5),
        lease=lease,
    )


def _sealed_plan(valid_plan: DeploymentPlan, *, tenant_id: str) -> SealedDeploymentPlan:
    return SealedDeploymentPlan(
        plan=valid_plan,
        plan_hash=valid_plan.content_hash(),
        tenant_id=tenant_id,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="chat",
        validity=ValidityWindow(not_before=NOW, not_after=NOW + timedelta(hours=1)),
        created_at=NOW,
    )


def _succeeded_run_result(deployment: Deployment) -> RunResult:
    return RunResult(
        deployment=deployment.model_copy(
            update={
                "status": DeploymentStatus.SUCCEEDED,
                "current_stage": None,
                "completed_at": NOW,
            }
        )
    )


def _halted_run_result(deployment: Deployment) -> RunResult:
    halted_deployment = deployment.model_copy(
        update={"status": DeploymentStatus.HALTED, "current_stage": None, "completed_at": NOW}
    )
    halted = HaltedState(
        failing_stage="infrastructure",
        error=StageError(code="ArmError", message="deployment stack failed", is_transient=False),
        recovery_options=("retry", "forward_fix"),
    )
    return RunResult(deployment=halted_deployment, halted=halted)


class _Harness:
    """One or more tenants' worth of Cosmos-backed repositories, plus the lease store — everything
    ``poll_once`` needs except the sequencer and credential factory, which each test supplies so it
    can script the exact outcome it wants to observe."""

    def __init__(self, tenant_ids: list[str]) -> None:
        self.tenant_registry = TenantRegistry(_FakeTenantRegistryContainer(tenant_ids))
        self.deployment_container = _FakeContainer()
        self.deployment_repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
            self.deployment_container, model_cls=Deployment, id_field="deployment_id"
        )
        self.plan_container = _FakeContainer()
        self.plan_repository: TenantScopedRepository[SealedDeploymentPlan] = TenantScopedRepository(
            self.plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
        )
        self.tenant_container = _FakeContainer()
        self.customer_tenant_repository: CustomerTenantRepository = TenantScopedRepository(
            self.tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
        )
        self.lease_store = SubscriptionLeaseStore(_FakeLeaseContainer())

    async def seed_tenant(
        self,
        tenant_id: str,
        *,
        devops_organization_url: str | None = ORG_URL,
        fabric_capacity_admin_upn: str | None = FABRIC_UPN,
    ) -> None:
        await self.customer_tenant_repository.create(
            tenant_id,
            CustomerTenant(
                tenant_id=tenant_id,
                display_name=f"tenant {tenant_id[:8]}",
                consent_state=ConsentState.GRANTED,
                consent_granted_at=NOW,
                approved_regions=frozenset({"australiaeast"}),
                data_residency_regions=frozenset({"australiaeast"}),
                devops_organization_url=devops_organization_url,
                fabric_capacity_admin_upn=fabric_capacity_admin_upn,
            ),
        )

    async def seed_deployment(self, deployment: Deployment) -> None:
        await self.deployment_repository.create(deployment.tenant_id, deployment)

    async def seed_plan(self, sealed: SealedDeploymentPlan) -> None:
        await self.plan_repository.create(sealed.tenant_id, sealed)

    async def read_deployment(self, tenant_id: str, deployment_id: str) -> Deployment:
        found = await self.deployment_repository.read(tenant_id, deployment_id)
        assert found is not None
        return found


async def _poll(
    harness: _Harness,
    sequencer: Any,
    credential_factory: Any,
    *,
    devops_organization_url_fallback: str | None = None,
    fabric_capacity_admin_upn_fallback: str | None = None,
    max_concurrent_deployments: int | None = None,
) -> list[Any]:
    return await poll_once(
        tenant_registry=harness.tenant_registry,
        deployment_repository=harness.deployment_repository,
        plan_repository=harness.plan_repository,
        customer_tenant_repository=harness.customer_tenant_repository,
        lease_store=harness.lease_store,
        sequencer=sequencer,
        credential_factory=credential_factory,
        now_fn=lambda: NOW,
        devops_organization_url_fallback=devops_organization_url_fallback,
        fabric_capacity_admin_upn_fallback=fabric_capacity_admin_upn_fallback,
        max_concurrent_deployments=max_concurrent_deployments,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_queued_deployment_is_picked_up_and_executed(valid_plan: DeploymentPlan) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert len(outcomes) == 1
    assert outcomes[0].result is DeploymentAttemptResult.EXECUTED
    assert outcomes[0].tenant_id == TENANT_ID
    assert outcomes[0].deployment_id == DEPLOYMENT_ID

    assert len(sequencer.calls) == 1
    executed_deployment, plan, credential = sequencer.calls[0]
    assert executed_deployment.status is DeploymentStatus.EXECUTING
    assert executed_deployment.lease is not None
    assert executed_deployment.lease.holder == DEPLOYMENT_ID
    assert executed_deployment.started_at == NOW
    assert executed_deployment.queue_position is None
    assert plan == valid_plan
    assert credential == f"credential-for-{TENANT_ID}"
    assert credential_factory.scoped_calls == [TENANT_ID]


async def test_deployment_with_lease_already_held_is_skipped_not_halted(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    # Someone else already holds this subscription's lease.
    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="some-other-deployment", now=NOW)

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert len(outcomes) == 1
    assert outcomes[0].result is DeploymentAttemptResult.LEASE_HELD
    assert sequencer.calls == []

    # The deployment itself was never touched — still queued, exactly as found.
    unchanged = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert unchanged.status is DeploymentStatus.QUEUED
    assert unchanged.lease is None


async def test_halted_run_still_releases_the_lease(valid_plan: DeploymentPlan) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_halted_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert outcomes[0].result is DeploymentAttemptResult.EXECUTED
    assert outcomes[0].run_result is not None
    assert outcomes[0].run_result.halted is not None

    # A different holder can now acquire the same subscription's lease immediately — proof the
    # halted run's lease was released, not left to expire on its own TTL.
    lease = await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="a-later-deployment", now=NOW)
    assert lease.holder == "a-later-deployment"


async def test_sequencer_raising_is_reported_as_error_and_still_releases_the_lease(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(RuntimeError("boom"))
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert outcomes[0].result is DeploymentAttemptResult.ERROR
    assert "boom" in outcomes[0].detail

    lease = await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="a-later-deployment", now=NOW)
    assert lease.holder == "a-later-deployment"


async def test_missing_plan_is_reported_and_deployment_left_queued(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    # No matching plan seeded at all.
    deployment = _queued_deployment(plan_hash=valid_plan.content_hash())
    await harness.seed_deployment(deployment)

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert outcomes[0].result is DeploymentAttemptResult.PLAN_MISSING
    assert sequencer.calls == []

    unchanged = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert unchanged.status is DeploymentStatus.QUEUED

    # Lease was still acquired-and-released around the attempt, even though it never reached the
    # sequencer.
    lease = await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="a-later-deployment", now=NOW)
    assert lease.holder == "a-later-deployment"


async def test_every_tenant_is_considered_not_just_the_first(valid_plan: DeploymentPlan) -> None:
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    deployment_a = _queued_deployment(plan_hash=plan_hash)
    deployment_b = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(deployment_a)
    await harness.seed_deployment(deployment_b)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    assert len(outcomes) == 2
    seen_tenants = {outcome.tenant_id for outcome in outcomes}
    assert seen_tenants == {TENANT_ID, OTHER_TENANT_ID}
    assert all(outcome.result is DeploymentAttemptResult.EXECUTED for outcome in outcomes)
    assert len(sequencer.calls) == 2
    assert set(credential_factory.scoped_calls) == {TENANT_ID, OTHER_TENANT_ID}


async def test_queued_deployments_within_a_tenant_are_attempted_in_queue_position_order(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()

    first = _queued_deployment(
        deployment_id=DEPLOYMENT_ID,
        subscription_id=SUBSCRIPTION_ID,
        plan_hash=plan_hash,
        queue_position=0,
    )
    second = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
        queue_position=1,
    )
    # Seed out of FIFO order to prove the loop sorts rather than relying on insertion order.
    await harness.seed_deployment(second)
    await harness.seed_deployment(first)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    await _poll(harness, sequencer, credential_factory)

    assert [call[0].deployment_id for call in sequencer.calls] == [
        DEPLOYMENT_ID,
        OTHER_DEPLOYMENT_ID,
    ]


async def test_lease_held_deployment_does_not_block_a_different_subscription_in_same_tenant(
    valid_plan: DeploymentPlan,
) -> None:
    """A subscription lease held for one deployment must not stop a *different* subscription's
    queued deployment, even within the same tenant."""
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()

    blocked = _queued_deployment(
        deployment_id=DEPLOYMENT_ID,
        subscription_id=SUBSCRIPTION_ID,
        plan_hash=plan_hash,
        queue_position=0,
    )
    free = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
        queue_position=1,
    )
    await harness.seed_deployment(blocked)
    await harness.seed_deployment(free)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="someone-else", now=NOW)

    sequencer = _FakeSequencer(_succeeded_run_result)
    credential_factory = _FakeCredentialFactory()

    outcomes = await _poll(harness, sequencer, credential_factory)

    results = {outcome.deployment_id: outcome.result for outcome in outcomes}
    assert results[DEPLOYMENT_ID] is DeploymentAttemptResult.LEASE_HELD
    assert results[OTHER_DEPLOYMENT_ID] is DeploymentAttemptResult.EXECUTED


async def test_tenant_record_engagement_data_reaches_the_sequencer(
    valid_plan: DeploymentPlan,
) -> None:
    """The customer's own organization URL and capacity-admin UPN travel from their
    CustomerTenant record into Sequencer.run — engagement data, not worker config."""
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    assert outcomes[0].result is DeploymentAttemptResult.EXECUTED
    assert sequencer.engagement == [(ORG_URL, FABRIC_UPN)]


async def test_no_engagement_data_anywhere_declines_not_halts(valid_plan: DeploymentPlan) -> None:
    """A deployment whose tenant has no engagement data and no worker-wide fallback is declined
    and left queued — a wait state an operator or a conversation can resolve by recording the
    values, never an error, and never a lease acquisition."""
    harness = _Harness([TENANT_ID])
    # Tenant record exists but carries neither engagement field.
    await harness.seed_tenant(
        TENANT_ID, devops_organization_url=None, fabric_capacity_admin_upn=None
    )
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    assert outcomes[0].result is DeploymentAttemptResult.AWAITING_TENANT_CONFIG
    assert "devops_organization_url" in outcomes[0].detail
    assert "fabric_capacity_admin_upn" in outcomes[0].detail
    assert sequencer.calls == []

    unchanged = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert unchanged.status is DeploymentStatus.QUEUED
    # Declined before lease acquisition: the subscription was never locked.
    lease = await harness.lease_store.acquire(SUBSCRIPTION_ID, holder="a-later-deployment", now=NOW)
    assert lease.holder == "a-later-deployment"


async def test_tenant_record_overrides_worker_wide_fallback(valid_plan: DeploymentPlan) -> None:
    """Tenant-record values win over the fallbacks; the fallback fills only the gap."""
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID, fabric_capacity_admin_upn=None)  # org only on record
    plan_hash = valid_plan.content_hash()
    deployment = _queued_deployment(plan_hash=plan_hash)
    await harness.seed_deployment(deployment)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(
        harness,
        sequencer,
        _FakeCredentialFactory(),
        devops_organization_url_fallback="https://dev.azure.com/master-tenant",
        fabric_capacity_admin_upn_fallback="fallback-admin@groundwork.example",
    )

    assert outcomes[0].result is DeploymentAttemptResult.EXECUTED
    # Org URL from the tenant record, UPN from the fallback.
    assert sequencer.engagement == [(ORG_URL, "fallback-admin@groundwork.example")]


async def test_expired_lease_orphaned_executing_deployment_is_recovered_to_queued(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    expired_lease = SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW - timedelta(minutes=1))
    deployment = _executing_deployment(plan_hash=plan_hash, lease=expired_lease)
    await harness.seed_deployment(deployment)

    outcomes = await _poll(harness, _FakeSequencer(_succeeded_run_result), _FakeCredentialFactory())

    assert len(outcomes) == 1
    assert outcomes[0].result is DeploymentAttemptResult.RECOVERED

    recovered = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert recovered.status is DeploymentStatus.QUEUED
    assert recovered.current_stage is None
    assert recovered.started_at is None
    assert recovered.completed_at is None
    assert recovered.lease is None
    assert Deployment.model_validate(recovered.model_dump()) == recovered


async def test_live_lease_executing_deployment_is_not_touched(valid_plan: DeploymentPlan) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    plan_hash = valid_plan.content_hash()
    live_lease = SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW + timedelta(minutes=10))
    deployment = _executing_deployment(plan_hash=plan_hash, lease=live_lease)
    await harness.seed_deployment(deployment)
    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder=DEPLOYMENT_ID, now=NOW)

    outcomes = await _poll(harness, _FakeSequencer(_succeeded_run_result), _FakeCredentialFactory())

    assert outcomes == []
    unchanged = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert unchanged == deployment


async def test_orphan_requeue_failure_is_logged_and_other_tenant_work_still_proceeds(
    valid_plan: DeploymentPlan, caplog: Any
) -> None:
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    orphan = _executing_deployment(
        plan_hash=plan_hash,
        lease=SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW - timedelta(minutes=1)),
    )
    queued_other = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(orphan)
    await harness.seed_deployment(queued_other)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))
    harness.deployment_container.fail_replace_ids.add(DEPLOYMENT_ID)

    sequencer = _FakeSequencer(_succeeded_run_result)
    caplog.set_level(logging.ERROR)

    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    results = {outcome.deployment_id: outcome.result for outcome in outcomes}
    assert results[DEPLOYMENT_ID] is DeploymentAttemptResult.ERROR
    assert results[OTHER_DEPLOYMENT_ID] is DeploymentAttemptResult.EXECUTED
    assert "orphaned deployment recovery failed" in caplog.text
    still_executing = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert still_executing.status is DeploymentStatus.EXECUTING
    assert [call[0].deployment_id for call in sequencer.calls] == [OTHER_DEPLOYMENT_ID]


async def test_requeue_after_recovery_choice_round_trips_through_validation(
    valid_plan: DeploymentPlan,
) -> None:
    executing = _executing_deployment(
        plan_hash=valid_plan.content_hash(),
        lease=SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW - timedelta(minutes=1)),
    ).model_copy(update={"completed_at": NOW})

    requeued = requeue_after_recovery_choice(executing)

    assert Deployment.model_validate(requeued.model_dump()) == requeued


async def test_recovered_orphan_does_not_block_healthy_queued_work_in_same_cycle(
    valid_plan: DeploymentPlan,
) -> None:
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    orphan = _executing_deployment(
        plan_hash=plan_hash,
        lease=SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW - timedelta(minutes=1)),
    )
    queued_other = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(orphan)
    await harness.seed_deployment(queued_other)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)

    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    assert [outcome.result for outcome in outcomes] == [
        DeploymentAttemptResult.RECOVERED,
        DeploymentAttemptResult.EXECUTED,
    ]
    recovered = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert recovered.status is DeploymentStatus.QUEUED
    assert [call[0].deployment_id for call in sequencer.calls] == [OTHER_DEPLOYMENT_ID]


async def test_poll_cycle_emits_structured_heartbeat_event(caplog) -> None:
    """FR-054 sibling: every poll cycle logs a `queue_poll_cycle` event even when there is
    nothing to do. observability.bicep's queue-poll-heartbeat alert fires on ABSENCE of this
    event, so the quiet path must be loud too."""
    import logging

    harness = _Harness(tenant_ids=[])

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    logger = logging.getLogger("groundwork_orchestrator.engine.queue_loop")
    handler = _Capture(level=logging.INFO)
    logger.addHandler(handler)
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    try:
        await _poll(
            harness,
            sequencer=_FakeSequencer(result=None),
            credential_factory=_FakeCredentialFactory(),
        )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)

    heartbeat = [r for r in records if getattr(r, "event", None) == "queue_poll_cycle"]
    assert heartbeat, "expected heartbeat event on an empty cycle"
    assert heartbeat[0].__dict__["tenants_considered"] == 0


async def test_platform_concurrency_cap_defers_queued_work_when_already_at_capacity(
    valid_plan: DeploymentPlan,
) -> None:
    """GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS (found live 2026-09-07 as validated-but-never-read):
    a deployment already EXECUTING with a live lease — the same shape
    test_live_lease_executing_deployment_is_not_touched already covers — counts against the
    platform-wide cap. A different tenant's queued deployment must be left queued, not attempted,
    once that cap is reached."""
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    live_lease = SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW + timedelta(minutes=10))
    already_executing = _executing_deployment(plan_hash=plan_hash, lease=live_lease)
    await harness.seed_deployment(already_executing)
    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder=DEPLOYMENT_ID, now=NOW)

    queued_elsewhere = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(queued_elsewhere)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(
        harness, sequencer, _FakeCredentialFactory(), max_concurrent_deployments=1
    )

    at_capacity = [
        o for o in outcomes if o.result is DeploymentAttemptResult.PLATFORM_AT_CAPACITY
    ]
    assert len(at_capacity) == 1
    assert at_capacity[0].deployment_id == OTHER_DEPLOYMENT_ID
    assert sequencer.calls == []  # never even attempted — no lease, no plan read, no run

    still_queued = await harness.read_deployment(OTHER_TENANT_ID, OTHER_DEPLOYMENT_ID)
    assert still_queued.status is DeploymentStatus.QUEUED


async def test_platform_concurrency_cap_of_none_leaves_behaviour_unbounded(
    valid_plan: DeploymentPlan,
) -> None:
    """The default (``max_concurrent_deployments=None``, matching every other existing test in
    this file) must reproduce today's unbounded behaviour exactly — this cap is opt-in."""
    harness = _Harness([TENANT_ID, OTHER_TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    await harness.seed_tenant(OTHER_TENANT_ID)
    plan_hash = valid_plan.content_hash()

    live_lease = SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW + timedelta(minutes=10))
    already_executing = _executing_deployment(plan_hash=plan_hash, lease=live_lease)
    await harness.seed_deployment(already_executing)
    await harness.lease_store.acquire(SUBSCRIPTION_ID, holder=DEPLOYMENT_ID, now=NOW)

    queued_elsewhere = _queued_deployment(
        deployment_id=OTHER_DEPLOYMENT_ID,
        tenant_id=OTHER_TENANT_ID,
        subscription_id=OTHER_SUBSCRIPTION_ID,
        plan_hash=plan_hash,
    )
    await harness.seed_deployment(queued_elsewhere)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=OTHER_TENANT_ID))

    sequencer = _FakeSequencer(_succeeded_run_result)
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())

    assert [o.result for o in outcomes] == [DeploymentAttemptResult.EXECUTED]
    assert len(sequencer.calls) == 1
