"""Append-only audit repository — write-before-action (T023; the audit-by-construction rule,
FR-047).

The audit-by-construction rule requires the audit record for a mutating action to be durably
committed *before* that action is attempted — a log entry written after the fact records what
happened, not what was authorised, and the two are not the same guarantee.
:class:`AuditRepository` makes that ordering the only path through it:
:meth:`AuditRepository.record_before` writes and awaits the audit record
before ever invoking the action, and if the write raises, the action is never called at all.

This repository never updates or deletes. There is no method on it that could — every operation is
:meth:`TenantScopedRepository.create`, and the append-only guarantee follows from that being the
only thing this class exposes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from groundwork_contracts.audit import AuditRecord
from groundwork_orchestrator.state.cosmos import CosmosStateStore, TenantScopedRepository

R = TypeVar("R")


class AuditRepository:
    """Wraps a tenant-scoped ``AuditRecord`` container with the write-before-action guarantee."""

    def __init__(self, repository: TenantScopedRepository[AuditRecord]) -> None:
        self._repository = repository

    async def record_before(
        self,
        tenant_id: str,
        record: AuditRecord,
        action: Callable[[], Awaitable[R]],
    ) -> R:
        """Durably commit ``record``, then run ``action`` (FR-047, audit-by-construction).

        ``record`` must already be a valid :class:`AuditRecord` — the type itself refuses to
        represent a mutating action with no authority chain, or one attributed to an agent (see
        ``groundwork_contracts.audit.AuditRecord``), so those defects are unrepresentable before
        this method is ever reached. What this method adds is the *ordering* guarantee: if the
        write to Cosmos fails or raises, ``action`` does not run, because the ``await`` below has
        not returned successfully yet.
        """
        await self._repository.create(tenant_id, record)
        return await action()

    async def append(self, tenant_id: str, record: AuditRecord) -> AuditRecord:
        """Write an audit record with no action to gate.

        For records that are not the sole authorisation of a following action — most commonly the
        follow-up ``succeeded`` / ``failed`` entry for a ``sequence`` a prior :meth:`record_before`
        call already opened, where by the time this is called the action has already run and there
        is nothing left to gate.
        """
        return await self._repository.create(tenant_id, record)


def audit_repository(store: CosmosStateStore) -> AuditRepository:
    return AuditRepository(store.repository("audit", model_cls=AuditRecord, id_field="audit_id"))
