"""T025 — the audit repository's write-before-action ordering (the audit-by-construction rule,
FR-047).

Two things must both be true, and this file is split to test them at the layer where each actually
lives:

1. An audit record for a mutating action cannot be *constructed* without an authority chain — that
   guarantee already lives on the type itself and is covered by
   ``tests/unit/test_audit_authority.py``. It is not re-tested here.
2. An action gated by :meth:`AuditRepository.record_before` cannot *run* unless its audit write has
   already durably committed — that is a repository-layer ordering guarantee, new in this file, and
   it is what these tests exist to prove.

A fake container stands in for Cosmos, same reasoning as ``tests/unit/test_cosmos_repository.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from pydantic import ValidationError

from groundwork_contracts.audit import (
    ActorIdentity,
    ActorType,
    AuditOutcome,
    AuditRecord,
    AuthorityChain,
)
from groundwork_orchestrator.state.audit_repository import AuditRepository
from groundwork_orchestrator.state.cosmos import TENANT_PARTITION_FIELD, TenantScopedRepository
from tests.conftest import APPROVAL_ID, APPROVER_ID, CORRELATION_ID, TENANT_ID

AUDIT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAN_HASH = "sha256:" + "e" * 64
NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class FakeContainer:
    """In-memory stand-in for ``azure.cosmos.aio.ContainerProxy``. Never touches Cosmos."""

    def __init__(self, *, fail_writes: bool = False) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self._fail_writes = fail_writes

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> Mapping[str, Any]:
        if self._fail_writes:
            raise CosmosResourceNotFoundError(status_code=503, message="simulated Cosmos outage")
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
            yield document


def _audit_record(**overrides: object) -> AuditRecord:
    kwargs: dict[str, object] = {
        "audit_id": AUDIT_ID,
        "tenant_id": TENANT_ID,
        "correlation_id": CORRELATION_ID,
        "sequence": 0,
        "occurred_at": NOW,
        "actor": ActorIdentity(
            object_id=APPROVER_ID, display_name="Orchestrator Worker", actor_type=ActorType.WORKLOAD
        ),
        "action": "deploy_infrastructure",
        "is_mutating": True,
        "authorised_by": AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        "outcome": AuditOutcome.AUTHORISED,
        "retention_expires_at": NOW + timedelta(days=365),
    }
    kwargs.update(overrides)
    return AuditRecord(**kwargs)  # type: ignore[arg-type]


# --- The repository-level ordering guarantee (new in this file) --------------------


async def test_action_runs_only_after_the_audit_write_commits() -> None:
    """The core T023/T025 guarantee: the action is not merely called last, it is called only once
    the write it depends on has actually completed."""
    container = FakeContainer()
    repository = AuditRepository(
        TenantScopedRepository(container, model_cls=AuditRecord, id_field="audit_id")
    )
    call_order: list[str] = []

    async def action() -> str:
        # If this runs, the audit document must already be visible in the container — proving the
        # write was awaited to completion, not merely started, before the action began.
        assert AUDIT_ID in container.documents
        call_order.append("action")
        return "provisioned"

    result = await repository.record_before(TENANT_ID, _audit_record(), action)

    assert call_order == ["action"]
    assert result == "provisioned"
    assert AUDIT_ID in container.documents


async def test_action_never_runs_if_the_audit_write_fails() -> None:
    """FR-047 / audit-by-construction: an action must not proceed on the strength of an audit
    write that did not actually commit. A durability failure here must not silently become
    'authorised anyway'."""
    container = FakeContainer(fail_writes=True)
    repository = AuditRepository(
        TenantScopedRepository(container, model_cls=AuditRecord, id_field="audit_id")
    )
    action_ran = False

    async def action() -> None:
        nonlocal action_ran
        action_ran = True

    with pytest.raises(CosmosResourceNotFoundError):
        await repository.record_before(TENANT_ID, _audit_record(), action)

    assert action_ran is False
    assert container.documents == {}


async def test_append_writes_with_no_action_to_gate() -> None:
    """The follow-up-entry path: a record written with nothing pending on it."""
    container = FakeContainer()
    repository = AuditRepository(
        TenantScopedRepository(container, model_cls=AuditRecord, id_field="audit_id")
    )

    written = await repository.append(TENANT_ID, _audit_record(outcome=AuditOutcome.SUCCEEDED))

    assert written.outcome is AuditOutcome.SUCCEEDED
    assert AUDIT_ID in container.documents


# --- The type-level guarantee this repository relies on but does not duplicate -----


def test_repository_cannot_be_handed_a_mutating_record_with_no_authority() -> None:
    """T025's second half: authorisedBy is required for any mutating action. Proven here by showing
    the repository never even gets the chance to accept one — construction fails first, which is the
    point: the repository does not need its own check because the type already makes the bad state
    unrepresentable (see tests/unit/test_audit_authority.py for the exhaustive version of this)."""
    with pytest.raises(ValidationError, match="no authority chain"):
        _audit_record(authorised_by=None)
