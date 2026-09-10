"""T022 — the tenant-scoped Cosmos repository base.

A fake container stands in for ``azure.cosmos.aio.ContainerProxy`` so these tests prove the
serialisation and partition-scoping logic in ``groundwork_orchestrator.state.cosmos`` without a live
Cosmos account — the same reasoning as ``tests/security/test_credential_scoping.py``'s fake
credential. FR-032 is what is under test: there must be no way to reach, read, or write outside the
tenant partition a caller names.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from groundwork_contracts.audit import (
    ActorIdentity,
    ActorType,
    AuditOutcome,
    AuditRecord,
    AuthorityChain,
)
from groundwork_orchestrator.state.cosmos import (
    TENANT_PARTITION_FIELD,
    TenantScopedRepository,
    from_document,
    to_document,
)
from tests.conftest import APPROVAL_ID, APPROVER_ID, CORRELATION_ID, TENANT_ID

OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
AUDIT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAN_HASH = "sha256:" + "e" * 64


class FakeContainer:
    """In-memory stand-in for ``azure.cosmos.aio.ContainerProxy``. Never touches Cosmos."""

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def read_item(self, item: str, partition_key: Any, **kwargs: Any) -> Mapping[str, Any]:
        document = self.documents.get(item)
        if document is None or document.get(TENANT_PARTITION_FIELD) != partition_key:
            raise CosmosResourceNotFoundError(status_code=404, message=f"{item} not found")
        return document

    async def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Mapping[str, Any]]:
        for document in self.documents.values():
            if partition_key is not None and document.get(TENANT_PARTITION_FIELD) != partition_key:
                continue
            yield document


def _audit_record(tenant_id: str = TENANT_ID) -> AuditRecord:
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    return AuditRecord(
        audit_id=AUDIT_ID,
        tenant_id=tenant_id,
        correlation_id=CORRELATION_ID,
        sequence=0,
        occurred_at=now,
        actor=ActorIdentity(
            object_id=APPROVER_ID, display_name="Orchestrator Worker", actor_type=ActorType.WORKLOAD
        ),
        action="deploy_infrastructure",
        is_mutating=True,
        authorised_by=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=AuditOutcome.AUTHORISED,
        retention_expires_at=now + timedelta(days=365),
    )


# --- Serialisation round-trip -------------------------------------------------------


def test_to_document_adds_id_and_tenant_partition_field() -> None:
    record = _audit_record()
    document = to_document(record, id_field="audit_id")

    assert document["id"] == AUDIT_ID
    assert document[TENANT_PARTITION_FIELD] == TENANT_ID
    # The model's own field is untouched — only the two Cosmos-required keys are added.
    assert document["tenant_id"] == TENANT_ID


def test_document_round_trips_back_to_the_same_model() -> None:
    record = _audit_record()
    document = to_document(record, id_field="audit_id")

    assert from_document(AuditRecord, document) == record


def test_free_form_dict_keys_survive_serialisation_unchanged() -> None:
    """Guards the reason a full key-casing transform was rejected: ``details`` keys are caller
    data, not field names, and must not be rewritten."""
    free_form = {"resource_group": "rg-example", "count": 3}
    record = _audit_record().model_copy(update={"details": free_form})
    document = to_document(record, id_field="audit_id")

    assert document["details"] == free_form
    assert from_document(AuditRecord, document).details == free_form


def test_cosmos_metadata_is_dropped_on_read() -> None:
    document = to_document(_audit_record(), id_field="audit_id")
    document["_rid"] = "abc123=="
    document["_etag"] = '"00000000-0000-0000-0000-000000000000"'
    document["_ts"] = 1785000000

    assert from_document(AuditRecord, document) == _audit_record()


# --- Tenant scoping (FR-032) ---------------------------------------------------------


async def test_create_writes_into_the_named_tenants_partition() -> None:
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )

    await repo.create(TENANT_ID, _audit_record())

    assert container.documents[AUDIT_ID][TENANT_PARTITION_FIELD] == TENANT_ID


async def test_create_refuses_a_document_whose_tenant_disagrees_with_the_caller() -> None:
    """FR-032 — a caller cannot write into a partition it did not name, even by accident."""
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )

    with pytest.raises(ValueError, match="does not match"):
        await repo.create(OTHER_TENANT_ID, _audit_record(tenant_id=TENANT_ID))

    assert container.documents == {}


async def test_read_cannot_see_a_document_in_a_different_tenants_partition() -> None:
    """The structural half of FR-032 / SC-011: reading with the wrong tenant finds nothing, it does
    not raise a permission error that could be caught and worked around — there is simply no
    document at that (item, partition) coordinate as far as this repository can see."""
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )
    await repo.create(TENANT_ID, _audit_record(tenant_id=TENANT_ID))

    same_tenant = await repo.read(TENANT_ID, AUDIT_ID)
    other_tenant = await repo.read(OTHER_TENANT_ID, AUDIT_ID)

    assert same_tenant == _audit_record(tenant_id=TENANT_ID)
    assert other_tenant is None


async def test_replace_overwrites_the_existing_document_in_place() -> None:
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )
    await repo.create(TENANT_ID, _audit_record(tenant_id=TENANT_ID))

    updated = _audit_record(tenant_id=TENANT_ID).model_copy(update={"action": "amended_action"})
    result = await repo.replace(TENANT_ID, updated)

    assert result.action == "amended_action"
    assert len(container.documents) == 1
    assert (await repo.read(TENANT_ID, AUDIT_ID)).action == "amended_action"  # type: ignore[union-attr]


async def test_replace_refuses_a_document_whose_tenant_disagrees_with_the_caller() -> None:
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )
    await repo.create(TENANT_ID, _audit_record(tenant_id=TENANT_ID))

    with pytest.raises(ValueError, match="does not match"):
        await repo.replace(OTHER_TENANT_ID, _audit_record(tenant_id=TENANT_ID))


async def test_query_only_returns_documents_from_the_named_tenant() -> None:
    container = FakeContainer()
    repo: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        container, model_cls=AuditRecord, id_field="audit_id"
    )
    await repo.create(TENANT_ID, _audit_record(tenant_id=TENANT_ID))

    other_record = _audit_record(tenant_id=OTHER_TENANT_ID).model_copy(
        update={"audit_id": "cccccccc-cccc-cccc-cccc-cccccccccccc"}
    )
    await repo.create(OTHER_TENANT_ID, other_record)

    results = [item async for item in repo.query(TENANT_ID, "SELECT * FROM c")]

    assert len(results) == 1
    assert results[0].tenant_id == TENANT_ID
