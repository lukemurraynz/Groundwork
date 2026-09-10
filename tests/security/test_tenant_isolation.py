"""T063 / SC-011 — tenant-scoped data and credentials never cross."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.identity.credentials import (
    CrossTenantAccessError,
    TenantScopedCredentialFactory,
)
from groundwork_shared.queue.subscription_lease import SubscriptionLeaseStore
from tests.conftest import APPROVAL_ID, CORRELATION_ID, OTHER_TENANT_ID, SUBSCRIPTION_ID, TENANT_ID
from tests.unit.test_subscription_lease import _FakeLeaseContainer

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 1, tzinfo=UTC)


class _FakeCredential(AsyncTokenCredential):
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        return object()

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _FakeCredential:
        return self

    async def __aexit__(
        self,
        exc_type: object | None = None,
        exc: object | None = None,
        tb: object | None = None,
    ) -> None:
        return None


class _LoggingContainer:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str, str]] = []

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("create", body["tenantId"], body["id"]))
        self.documents[(body["tenantId"], body["id"])] = body
        return body

    async def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("upsert", body["tenantId"], body["id"]))
        self.documents[(body["tenantId"], body["id"])] = body
        return body

    async def read_item(self, item: str, partition_key: Any, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("read", str(partition_key), item))
        return self.documents[(str(partition_key), item)]

    async def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[dict[str, Any]]:
        partition = str(partition_key)
        self.calls.append(("query", partition, query))
        for (tenant_id, _item_id), document in self.documents.items():
            if tenant_id == partition:
                yield document


def _deployment(*, tenant_id: str, subscription_id: str, deployment_id: str) -> Deployment:
    return Deployment(
        deployment_id=deployment_id,
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=f"sha256:{'a' * 64}", approval_id=APPROVAL_ID),
        status=DeploymentStatus.QUEUED,
        queue_position=0,
    )


async def test_repository_queries_are_partition_scoped() -> None:
    container = _LoggingContainer()
    repo: TenantScopedRepository[Deployment] = TenantScopedRepository(
        container, model_cls=Deployment, id_field="deployment_id"
    )
    deployment_a = _deployment(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        deployment_id="88888888-8888-8888-8888-888888888888",
    )
    deployment_b = _deployment(
        tenant_id=OTHER_TENANT_ID,
        subscription_id="44444444-4444-4444-4444-444444444444",
        deployment_id="99999999-9999-9999-9999-999999999999",
    )
    await repo.create(TENANT_ID, deployment_a)
    await repo.create(OTHER_TENANT_ID, deployment_b)

    read_a = await repo.read(TENANT_ID, deployment_a.deployment_id)
    read_b = await repo.read(OTHER_TENANT_ID, deployment_b.deployment_id)
    query_a = [item async for item in repo.query(TENANT_ID, "SELECT * FROM c")]
    query_b = [item async for item in repo.query(OTHER_TENANT_ID, "SELECT * FROM c")]

    assert read_a == deployment_a
    assert read_b == deployment_b
    assert [item.tenant_id for item in query_a] == [TENANT_ID]
    assert [item.tenant_id for item in query_b] == [OTHER_TENANT_ID]
    assert ("read", TENANT_ID, deployment_a.deployment_id) in container.calls
    assert ("read", OTHER_TENANT_ID, deployment_b.deployment_id) in container.calls
    assert ("query", TENANT_ID, "SELECT * FROM c") in container.calls
    assert ("query", OTHER_TENANT_ID, "SELECT * FROM c") in container.calls


def test_credential_factory_refuses_cross_tenant_use() -> None:
    factory = TenantScopedCredentialFactory(credential=_FakeCredential())
    scoped_a = factory.scoped_to(TENANT_ID)
    scoped_b = factory.scoped_to(OTHER_TENANT_ID)

    assert scoped_a.for_tenant(TENANT_ID) is not None
    assert scoped_b.for_tenant(OTHER_TENANT_ID) is not None
    with pytest.raises(CrossTenantAccessError):
        scoped_a.for_tenant(OTHER_TENANT_ID)
    with pytest.raises(CrossTenantAccessError):
        scoped_b.for_tenant(TENANT_ID)


async def test_subscription_leases_are_isolated_per_subscription() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())

    lease_a = await store.acquire(SUBSCRIPTION_ID, holder="dep-a", now=NOW)
    lease_b = await store.acquire("44444444-4444-4444-4444-444444444444", holder="dep-b", now=NOW)

    assert lease_a.holder == "dep-a"
    assert lease_b.holder == "dep-b"


async def test_two_tenants_keep_separate_mutable_deployment_state() -> None:
    container = _LoggingContainer()
    repo: TenantScopedRepository[Deployment] = TenantScopedRepository(
        container, model_cls=Deployment, id_field="deployment_id"
    )
    deployment_a = _deployment(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        deployment_id="88888888-8888-8888-8888-888888888888",
    ).model_copy(
        update={
            "status": DeploymentStatus.EXECUTING,
            "queue_position": None,
            "started_at": NOW,
            "lease": SubscriptionLease(holder="dep-a", expires_at=NOW + timedelta(hours=1)),
        }
    )
    deployment_b = _deployment(
        tenant_id=OTHER_TENANT_ID,
        subscription_id="44444444-4444-4444-4444-444444444444",
        deployment_id="99999999-9999-9999-9999-999999999999",
    )
    await repo.create(TENANT_ID, deployment_a)
    await repo.create(OTHER_TENANT_ID, deployment_b)

    updated_b = deployment_b.model_copy(update={"queue_position": 1})
    await repo.replace(OTHER_TENANT_ID, updated_b)

    persisted_a = await repo.read(TENANT_ID, deployment_a.deployment_id)
    persisted_b = await repo.read(OTHER_TENANT_ID, deployment_b.deployment_id)
    assert persisted_a is not None
    assert persisted_b is not None
    assert persisted_a == deployment_a
    assert persisted_b == updated_b
    assert Deployment.model_validate(persisted_a.model_dump()) == persisted_a
    assert Deployment.model_validate(persisted_b.model_dump()) == persisted_b
