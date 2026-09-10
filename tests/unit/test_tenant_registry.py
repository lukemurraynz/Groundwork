"""``TenantRegistry`` — the one deliberate cross-partition query in this codebase (added while
wiring the orchestrator's queue-consumption loop). See its own docstring in
``groundwork_orchestrator.state.cosmos`` for why it exists and why it is scoped the way it is.

The fake container here yields raw scalar values, matching real Cosmos ``SELECT VALUE x FROM c``
behaviour — deliberately not ``tests/unit/test_cosmos_repository.py``'s ``FakeContainer``, which
models full-document queries instead and would misrepresent this specific query shape.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from groundwork_orchestrator.state.cosmos import TenantRegistry

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


class _FakeValueContainer:
    def __init__(self, values: list[str]) -> None:
        self._values = values
        self.queries: list[str] = []

    async def query_items(
        self, query: str, *, parameters: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[str]:
        self.queries.append(query)
        for value in self._values:
            yield value


async def test_lists_every_tenant_id() -> None:
    container = _FakeValueContainer([TENANT_A, TENANT_B])
    registry = TenantRegistry(container)

    tenant_ids = [tenant_id async for tenant_id in registry.list_tenant_ids()]

    assert tenant_ids == [TENANT_A, TENANT_B]


async def test_query_selects_only_the_tenant_id_column() -> None:
    """This must never become ``SELECT * FROM c`` — that would pull full tenant documents
    (display name, consent state, concurrency cap) into a component whose entire justification is
    reading nothing but which partitions exist."""
    container = _FakeValueContainer([])
    registry = TenantRegistry(container)

    _ = [tenant_id async for tenant_id in registry.list_tenant_ids()]

    assert len(container.queries) == 1
    assert container.queries[0] == "SELECT VALUE c.tenantId FROM c"


async def test_empty_registry_yields_nothing() -> None:
    registry = TenantRegistry(_FakeValueContainer([]))

    tenant_ids = [tenant_id async for tenant_id in registry.list_tenant_ids()]

    assert tenant_ids == []
