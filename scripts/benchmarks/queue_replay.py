#!/usr/bin/env python
# ruff: noqa: E402
# /// script
# requires-python = ">=3.12"
# ///
"""Local in-memory replay harness for the orchestrator queue loop.

# ─── How to run ───
# .venv/Scripts/python.exe scripts/benchmarks/queue_replay.py \
#   --tenants 10 --deployments-per-tenant 5
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import Counter
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.blueprint import FabricCapacitySku
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import (
    ApprovedRegion,
    CostEstimateRef,
    DeploymentPlan,
    Environment,
    PlanResource,
    RiskAssessment,
    RiskSeverity,
    SealedDeploymentPlan,
    ValidityWindow,
)
from groundwork_contracts.tenant import ConsentState, CustomerTenant
from groundwork_orchestrator.engine.queue_loop import DeploymentAttemptOutcome, poll_once
from groundwork_orchestrator.engine.sequencer import RunResult
from groundwork_orchestrator.state.cosmos import TenantRegistry, TenantScopedRepository
from groundwork_shared.queue.subscription_lease import SubscriptionLeaseStore

REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
START_TIME = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)
ORG_URL = "https://dev.azure.com/groundwork-benchmark"
FABRIC_UPN = "capacity-admin@benchmark.example"


class _FakeContainer:
    """Minimal in-memory Cosmos stand-in copied from queue-loop tests."""

    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return str(self._etag_counter)

    @staticmethod
    def _key(document: Mapping[str, Any]) -> tuple[str, str]:
        return (str(document["tenantId"]), str(document["id"]))

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
        found = self.documents.get((str(partition_key), item))
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
        del parameters
        for document in self.documents.values():
            if partition_key is not None and document.get("tenantId") != partition_key:
                continue
            if "queued" in query and document.get("status") != "queued":
                continue
            if "executing" in query and document.get("status") != "executing":
                continue
            yield document


class _FakeTenantRegistryContainer:
    def __init__(self, tenant_ids: list[str]) -> None:
        self._tenant_ids = tenant_ids

    def query_items(
        self, query: str, *, parameters: list[dict[str, Any]] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        del query, parameters

        async def _iterator() -> AsyncIterator[str]:
            for tenant_id in self._tenant_ids:
                yield tenant_id

        return _iterator()


class _FakeLeaseContainer:
    """Minimal in-memory lease container copied from queue-loop tests."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return str(self._etag_counter)

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        item_id = str(body["id"])
        if item_id in self.documents:
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[item_id] = stored
        return stored

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        del partition_key
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


@dataclass(slots=True)
class _SteppedClock:
    current: datetime
    step: timedelta = timedelta(milliseconds=1)

    def now(self) -> datetime:
        moment = self.current
        self.current += self.step
        return moment


class _FakeScopedCredential:
    def __init__(self, tenant_id: str) -> None:
        self._tenant_id = tenant_id

    def for_tenant(self, tenant_id: str) -> str:
        if tenant_id != self._tenant_id:
            raise ValueError(f"unexpected tenant {tenant_id}")
        return f"credential-for-{tenant_id}"


class _FakeCredentialFactory:
    def scoped_to(self, tenant_id: str) -> _FakeScopedCredential:
        return _FakeScopedCredential(tenant_id)


class _PersistingSequencer:
    """Enough sequencer behaviour to let poll_once drain the queue."""

    def __init__(
        self, deployment_repository: TenantScopedRepository[Deployment], clock: _SteppedClock
    ) -> None:
        self._deployment_repository = deployment_repository
        self._clock = clock

    async def run(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        *,
        credential: str,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult:
        del plan, credential, devops_organization_url, fabric_capacity_admin_upn
        completed = deployment.model_copy(
            update={
                "status": DeploymentStatus.SUCCEEDED,
                "current_stage": None,
                "completed_at": self._clock.now(),
            }
        )
        stored = await self._deployment_repository.replace(deployment.tenant_id, completed)
        return RunResult(deployment=stored)


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    tenants: int
    deployments_per_tenant: int
    total_deployments: int
    cycles: int
    wall_clock_seconds: float
    deployments_per_second: float
    outcomes: Counter[str]


class _Harness:
    def __init__(self, tenant_ids: list[str]) -> None:
        self.tenant_ids = tenant_ids
        self.tenant_registry = TenantRegistry(_FakeTenantRegistryContainer(tenant_ids))
        self.deployment_repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
            _FakeContainer(), model_cls=Deployment, id_field="deployment_id"
        )
        self.plan_repository: TenantScopedRepository[SealedDeploymentPlan] = TenantScopedRepository(
            _FakeContainer(), model_cls=SealedDeploymentPlan, id_field="plan_hash"
        )
        self.customer_tenant_repository: TenantScopedRepository[CustomerTenant] = (
            TenantScopedRepository(_FakeContainer(), model_cls=CustomerTenant, id_field="tenant_id")
        )
        self.lease_store = SubscriptionLeaseStore(_FakeLeaseContainer())

    async def seed(self, *, tenants: int, deployments_per_tenant: int) -> None:
        for tenant_index in range(tenants):
            tenant_id = _tenant_id(tenant_index)
            await self.customer_tenant_repository.create(
                tenant_id,
                CustomerTenant(
                    tenant_id=tenant_id,
                    display_name=f"Benchmark tenant {tenant_index + 1}",
                    consent_state=ConsentState.GRANTED,
                    consent_granted_at=START_TIME,
                    approved_regions=frozenset({"australiaeast"}),
                    data_residency_regions=frozenset({"australiaeast"}),
                    devops_organization_url=ORG_URL,
                    fabric_capacity_admin_upn=FABRIC_UPN,
                ),
            )
            for deployment_index in range(deployments_per_tenant):
                sealed = _sealed_plan(
                    tenant_id=tenant_id,
                    tenant_index=tenant_index,
                    deployment_index=deployment_index,
                )
                await self.plan_repository.create(tenant_id, sealed)
                await self.deployment_repository.create(
                    tenant_id,
                    Deployment(
                        deployment_id=_deployment_id(tenant_index, deployment_index),
                        tenant_id=tenant_id,
                        subscription_id=_subscription_id(tenant_index, deployment_index),
                        correlation_id=_correlation_id(tenant_index, deployment_index),
                        authority=AuthorityChain(
                            plan_hash=sealed.plan_hash, approval_id=APPROVAL_ID
                        ),
                        status=DeploymentStatus.QUEUED,
                        queue_position=deployment_index,
                    ),
                )

    async def has_active_deployments(self) -> bool:
        for tenant_id in self.tenant_ids:
            async for _ in self.deployment_repository.query(
                tenant_id, "SELECT * FROM c WHERE c.status = 'queued'"
            ):
                return True
            async for _ in self.deployment_repository.query(
                tenant_id, "SELECT * FROM c WHERE c.status = 'executing'"
            ):
                return True
        return False


def _tenant_id(index: int) -> str:
    return f"{index + 1:08d}-0000-0000-0000-000000000000"


def _deployment_id(tenant_index: int, deployment_index: int) -> str:
    return f"{tenant_index + 1:08d}-1111-2222-3333-{deployment_index + 1:012d}"


def _subscription_id(tenant_index: int, deployment_index: int) -> str:
    serial = tenant_index * 1000 + deployment_index + 1
    return f"{serial:08d}-4444-5555-6666-{serial:012d}"


def _correlation_id(tenant_index: int, deployment_index: int) -> str:
    serial = tenant_index * 1000 + deployment_index + 1
    return f"{serial:08d}-7777-8888-9999-{serial:012d}"


def _sealed_plan(
    *, tenant_id: str, tenant_index: int, deployment_index: int
) -> SealedDeploymentPlan:
    plan = DeploymentPlan(
        blueprint_id="standard-production-fabric",
        blueprint_version="1.0.0",
        subscription_id=_subscription_id(tenant_index, deployment_index),
        region=ApprovedRegion.AUSTRALIA_EAST,
        environment=Environment.PRODUCTION,
        fabric_capacity_sku=FabricCapacitySku.F2,
        resource_set=(
            PlanResource(
                resource_type="Microsoft.Resources/resourceGroups",
                logical_name=f"rg-gw-{tenant_index + 1:03d}-{deployment_index + 1:03d}",
            ),
            PlanResource(
                resource_type="Microsoft.Fabric/capacities",
                logical_name=f"fab-gw-{tenant_index + 1:03d}-{deployment_index + 1:03d}",
                depends_on=(f"rg-gw-{tenant_index + 1:03d}-{deployment_index + 1:03d}",),
                properties={"sku": "F2", "adminUser": "platform-admin"},
            ),
        ),
        estimated_duration_minutes=45,
        cost_estimate=CostEstimateRef(
            monthly_total=412.5,
            uncertainty_lower_pct=10.0,
            uncertainty_upper_pct=20.0,
            basis="queue replay harness",
            computed_at=START_TIME,
        ),
        risk_assessment=RiskAssessment(severity=RiskSeverity.LOW),
    )
    return SealedDeploymentPlan(
        plan=plan,
        plan_hash=plan.content_hash(),
        tenant_id=tenant_id,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="benchmark",
        validity=ValidityWindow(not_before=START_TIME, not_after=START_TIME + timedelta(hours=1)),
        created_at=START_TIME,
    )


async def run_benchmark(*, tenants: int, deployments_per_tenant: int) -> BenchmarkResult:
    tenant_ids = [_tenant_id(index) for index in range(tenants)]
    harness = _Harness(tenant_ids)
    await harness.seed(tenants=tenants, deployments_per_tenant=deployments_per_tenant)

    clock = _SteppedClock(current=START_TIME)
    sequencer = _PersistingSequencer(harness.deployment_repository, clock)
    credential_factory = _FakeCredentialFactory()
    outcomes = Counter[str]()
    cycles = 0
    started = perf_counter()

    while await harness.has_active_deployments():
        cycle_outcomes = await poll_once(
            tenant_registry=harness.tenant_registry,
            deployment_repository=harness.deployment_repository,
            plan_repository=harness.plan_repository,
            customer_tenant_repository=harness.customer_tenant_repository,
            lease_store=harness.lease_store,
            sequencer=sequencer,
            credential_factory=credential_factory,
            now_fn=clock.now,
        )
        cycles += 1
        outcomes.update(_count_outcomes(cycle_outcomes))

    elapsed = perf_counter() - started
    total_deployments = tenants * deployments_per_tenant
    per_second = total_deployments / elapsed if elapsed else 0.0
    return BenchmarkResult(
        tenants=tenants,
        deployments_per_tenant=deployments_per_tenant,
        total_deployments=total_deployments,
        cycles=cycles,
        wall_clock_seconds=elapsed,
        deployments_per_second=per_second,
        outcomes=outcomes,
    )


def _count_outcomes(outcomes: list[DeploymentAttemptOutcome]) -> Counter[str]:
    return Counter(outcome.result.value for outcome in outcomes)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenants", type=int, required=True)
    parser.add_argument("--deployments-per-tenant", type=int, required=True)
    return parser.parse_args()


def _print_result(result: BenchmarkResult) -> None:
    print("queue replay benchmark")
    print(f"tenants: {result.tenants}")
    print(f"deployments_per_tenant: {result.deployments_per_tenant}")
    print(f"total_deployments: {result.total_deployments}")
    print(f"cycles: {result.cycles}")
    print(f"wall_clock_seconds: {result.wall_clock_seconds:.6f}")
    print(f"deployments_per_second: {result.deployments_per_second:.2f}")
    print("outcomes:")
    for outcome, count in sorted(result.outcomes.items()):
        print(f"  {outcome}: {count}")


async def _main_async() -> int:
    args = _parse_args()
    result = await run_benchmark(
        tenants=args.tenants,
        deployments_per_tenant=args.deployments_per_tenant,
    )
    _print_result(result)
    return 0


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())
