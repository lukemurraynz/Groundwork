"""T071 — durable checkpoint persistence (FR-030, FR-035)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_orchestrator.engine.checkpoints import record_checkpoint
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from tests.conftest import APPROVAL_ID, SUBSCRIPTION_ID, TENANT_ID

DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
PLAN_HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 1, tzinfo=UTC)
_RESUME_TOKEN = "tok"  # noqa: S105 -- an opaque stage-resume marker, not a credential


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        return self.documents[item]


def _executing_deployment() -> Deployment:
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        status=DeploymentStatus.EXECUTING,
        started_at=NOW,
        current_stage="infrastructure",
        lease=SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=NOW + timedelta(hours=1)),
    )


async def test_record_checkpoint_persists_the_completed_stage_and_clears_current_stage() -> None:
    container = _FakeContainer()
    repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
        container, model_cls=Deployment, id_field="deployment_id"
    )
    deployment = _executing_deployment()
    await repository.create(TENANT_ID, deployment)

    later = NOW + timedelta(minutes=5)
    updated = await record_checkpoint(
        repository,
        deployment,
        completed_stage="infrastructure",
        resume_token=_RESUME_TOKEN,
        now=later,
    )

    assert updated.checkpoint is not None
    assert updated.checkpoint.stage_name == "infrastructure"
    assert updated.checkpoint.resume_token == _RESUME_TOKEN
    assert updated.checkpoint.recorded_at == later
    assert updated.current_stage is None
    # Status and lease are untouched — record_checkpoint only ever updates checkpoint state.
    assert updated.status is DeploymentStatus.EXECUTING
    assert updated.lease == deployment.lease


async def test_record_checkpoint_is_read_back_correctly_from_storage() -> None:
    container = _FakeContainer()
    repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
        container, model_cls=Deployment, id_field="deployment_id"
    )
    deployment = _executing_deployment()
    await repository.create(TENANT_ID, deployment)

    await record_checkpoint(
        repository,
        deployment,
        completed_stage="infrastructure",
        resume_token=_RESUME_TOKEN,
        now=NOW,
    )

    reread = await repository.read(TENANT_ID, DEPLOYMENT_ID)
    assert reread is not None
    assert reread.checkpoint is not None
    assert reread.checkpoint.stage_name == "infrastructure"
