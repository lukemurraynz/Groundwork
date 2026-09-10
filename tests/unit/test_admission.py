"""T068 — per-tenant concurrency admission (FR-045a).

FR-045a's requirement, restated as the property under test: a tenant at its cap is queued, never
rejected, and told its position — not left to assume its request vanished or is already running.
"""

from __future__ import annotations

from datetime import UTC, datetime

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_controlplane.queue.admission import compute_admission, evaluate_admission
from groundwork_orchestrator.state.cosmos import TenantScopedRepository

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
PLAN_HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 1, tzinfo=UTC)


# --- compute_admission (pure) -----------------------------------------------------------


def test_below_cap_is_admitted_without_queueing() -> None:
    assert compute_admission(active_count=1, queued_count=0, concurrency_cap=3) is None


def test_at_cap_queues_at_position_one() -> None:
    assert compute_admission(active_count=3, queued_count=0, concurrency_cap=3) == 1


def test_above_cap_with_others_already_queued_ranks_behind_them() -> None:
    assert compute_admission(active_count=5, queued_count=2, concurrency_cap=3) == 3


def test_position_counts_only_queued_not_executing() -> None:
    """Two executing, one queued, cap 2 — the new arrival ranks behind the one queued deployment,
    not behind all three active ones."""
    assert compute_admission(active_count=3, queued_count=1, concurrency_cap=2) == 2


# --- evaluate_admission (repository-querying wrapper) -----------------------------------


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict] = {}

    async def create_item(self, body, **_):
        self.documents[(body["tenantId"], body["id"])] = body
        return body

    async def read_item(self, item, partition_key, **_):
        raise NotImplementedError

    async def query_items(self, query, *, parameters=None, partition_key=None, **_):
        for (tenant, _item_id), doc in self.documents.items():
            if tenant == partition_key:
                yield doc


def _deployment(deployment_id: str, status: DeploymentStatus, **overrides: object) -> Deployment:
    kwargs: dict[str, object] = {
        "deployment_id": deployment_id,
        "tenant_id": TENANT_ID,
        "subscription_id": SUBSCRIPTION_ID,
        "correlation_id": CORRELATION_ID,
        "authority": AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        "status": status,
    }
    if status is DeploymentStatus.EXECUTING:
        kwargs["started_at"] = NOW
        kwargs["lease"] = SubscriptionLease(holder=deployment_id, expires_at=NOW)
    kwargs.update(overrides)
    return Deployment(**kwargs)  # type: ignore[arg-type]


async def test_evaluate_admission_counts_only_this_tenants_active_deployments() -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[Deployment] = TenantScopedRepository(
        container, model_cls=Deployment, id_field="deployment_id"
    )
    other_tenant = "22222222-2222-2222-2222-222222222222"

    await repo.create(
        TENANT_ID,
        _deployment("44444444-4444-4444-4444-444444444444", DeploymentStatus.EXECUTING),
    )
    await repo.create(
        other_tenant,
        _deployment(
            "55555555-5555-5555-5555-555555555555",
            DeploymentStatus.EXECUTING,
            tenant_id=other_tenant,
        ),
    )

    position = await evaluate_admission(repo, TENANT_ID, concurrency_cap=1)

    assert position == 1  # only the same-tenant deployment counted toward the cap
