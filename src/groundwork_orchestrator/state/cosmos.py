"""Cosmos client and the tenant-scoped repository base (T022; FR-032, FR-047; original design
notes).

This module talks to a real ``azure.cosmos.aio.CosmosClient``, and nothing stands in for it on
the production path. Tests exercise the serialisation and
partition-scoping logic in this file against an in-memory fake container — the fake proves *this
module's* logic, not Cosmos itself, the same reasoning already applied to
``groundwork_shared.identity.credentials``'s ``CredentialProvider`` protocol.

FR-032's structural half lives here. Every :class:`TenantScopedRepository` method requires
``tenant_id`` as an argument and passes it straight through as the Cosmos ``partition_key`` — there
is no overload, default, or code path that can issue a request scoped to anything other than the
caller-supplied tenant. Per the SDK, a ``None`` partition key is what triggers a cross-partition
fan-out; supplying a concrete one is itself what scopes a call to one partition, so there is nothing
to "forget" to add.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Mapping, Sequence
from typing import Any, Protocol

from azure.cosmos.aio import CosmosClient
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from pydantic import BaseModel

# The literal partition key path every tenant-scoped container in infra/modules/cosmos.bicep
# declares. Not a parameter: FR-032 is exactly the guarantee that no caller can choose a different
# partitioning scheme for a tenant-scoped document.
TENANT_PARTITION_FIELD = "tenantId"

DEFAULT_DATABASE_NAME = "groundwork"


class ContainerLike(Protocol):
    """The subset of ``azure.cosmos.aio.ContainerProxy`` this module depends on.

    A protocol rather than importing ``ContainerProxy`` at every call site, so tests can substitute
    an in-memory fake without touching a real Cosmos account — the fake is test-only code, per the
    standing rule that mocks belong in tests, never in the production path.
    """

    def create_item(self, body: dict[str, Any], **kwargs: Any) -> Awaitable[Mapping[str, Any]]: ...

    def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> Awaitable[Mapping[str, Any]]: ...

    def read_item(
        self, item: str, partition_key: Any, **kwargs: Any
    ) -> Awaitable[Mapping[str, Any]]: ...

    def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **kwargs: Any,
    ) -> AsyncIterator[Mapping[str, Any]]: ...


def to_document(
    model: BaseModel, *, id_field: str, tenant_field: str = "tenant_id"
) -> dict[str, Any]:
    """Serialise a contracts model into a Cosmos document.

    Cosmos requires a top-level ``id`` and the partition key value at the exact property name the
    container's partition key path names — ``tenantId``, per ``infra/modules/cosmos.bicep``.
    Everything else is written using this package's own snake_case field names, unchanged.

    That is deliberate, not an oversight against the original design notes' illustrative
    camelCase: a general snake_case-to-camelCase transform would also have to walk into
    free-form fields like
    ``AuditRecord.details`` and ``PlanResource.properties``, whose keys are caller-supplied data,
    not field names, and are not safe to rewrite. Limiting the rename to the two fields Cosmos
    actually requires (``id``, the partition key) avoids that risk entirely.
    """
    payload = model.model_dump(mode="json")
    if id_field not in payload:
        raise ValueError(
            f"{type(model).__name__} has no field {id_field!r} to use as the Cosmos id"
        )
    if tenant_field not in payload:
        raise ValueError(
            f"{type(model).__name__} has no field {tenant_field!r} to use as the partition "
            f"key source"
        )
    document = dict(payload)
    document["id"] = payload[id_field]
    document[TENANT_PARTITION_FIELD] = payload[tenant_field]
    return document


def from_document[T: BaseModel](model_cls: type[T], document: Mapping[str, Any]) -> T:
    """Reconstruct a contracts model from a Cosmos document. Inverse of :func:`to_document`.

    Drops ``id`` and the ``tenantId`` duplicate Cosmos requires, plus any Cosmos-internal
    underscore-prefixed metadata (``_rid``, ``_etag``, ``_ts``, ...) — none of these are fields on
    the model, and the model's own ``extra="forbid"`` would otherwise reject them.
    """
    data = {
        key: value
        for key, value in document.items()
        if key != "id" and key != TENANT_PARTITION_FIELD and not key.startswith("_")
    }
    return model_cls.model_validate(data)


class TenantScopedRepository[T: BaseModel]:
    """Base for a container partitioned on ``/tenantId`` (T022).

    Every method requires ``tenant_id``. That is the FR-032 enforcement point at the data layer:
    there is no method here that can read, write, or query outside the caller-named tenant's
    partition, because there is no parameter through which a caller could ask for a different one.
    """

    def __init__(self, container: ContainerLike, *, model_cls: type[T], id_field: str) -> None:
        self._container = container
        self._model_cls = model_cls
        self._id_field = id_field

    async def create(self, tenant_id: str, model: T) -> T:
        """Create ``model`` in ``tenant_id``'s partition.

        Refuses to write a document whose own ``tenant_id`` field disagrees with the partition the
        caller named — a mismatch here means something upstream computed the wrong tenant, which is
        exactly the class of bug FR-032 requires be structurally prevented, not merely logged.
        """
        document = to_document(model, id_field=self._id_field)
        if document[TENANT_PARTITION_FIELD] != tenant_id:
            raise ValueError(
                f"model's tenant_id {document[TENANT_PARTITION_FIELD]!r} does not match the "
                f"tenant_id {tenant_id!r} this write was scoped to — refusing to write a document "
                f"into a partition the caller did not name (FR-032)"
            )
        created = await self._container.create_item(body=document)
        return from_document(self._model_cls, created)

    async def replace(self, tenant_id: str, model: T) -> T:
        """Replace an existing document in ``tenant_id``'s partition with ``model``'s current state.

        Uses Cosmos ``upsert_item`` rather than a read-modify-write pair: the caller already read
        the document it is replacing (to build the updated ``T``, since these models are frozen
        and replacing one field means constructing a new instance), so a second read here would
        only add a race window between it and the write. Same FR-032 tenant-match guard as
        :meth:`create`.
        """
        document = to_document(model, id_field=self._id_field)
        if document[TENANT_PARTITION_FIELD] != tenant_id:
            raise ValueError(
                f"model's tenant_id {document[TENANT_PARTITION_FIELD]!r} does not match the "
                f"tenant_id {tenant_id!r} this write was scoped to — refusing to write a document "
                f"into a partition the caller did not name (FR-032)"
            )
        replaced = await self._container.upsert_item(body=document)
        return from_document(self._model_cls, replaced)

    async def read_with_etag(self, tenant_id: str, item_id: str) -> tuple[T, str | None] | None:
        """Like :meth:`read`, but also returns the document's current ETag.

        Use with :meth:`replace_with_etag` to perform an optimistic-concurrency read-modify-write:
        pass the returned ETag back to ``replace_with_etag`` so Cosmos rejects the write if another
        writer has modified the document since this read. Returns ``None`` if the document does not
        exist.
        """
        try:
            document = await self._container.read_item(item=item_id, partition_key=tenant_id)
        except CosmosResourceNotFoundError:
            return None
        etag: str | None = document.get("_etag")
        return from_document(self._model_cls, document), etag

    async def replace_with_etag(self, tenant_id: str, model: T, *, etag: str | None) -> T:
        """Replace a document using ETag-based optimistic concurrency.

        When ``etag`` is supplied, the Cosmos write uses ``IfNotModified`` — the SDK raises
        ``azure.cosmos.exceptions.CosmosHttpResponseError`` (HTTP 412) if another writer has
        modified the document since the ETag was read. Callers are responsible for catching that
        and retrying with a fresh read. When ``etag`` is ``None``, behaves like :meth:`replace`
        (unconditional upsert).

        Same FR-032 tenant-match guard as :meth:`create`.
        """
        from azure.core import MatchConditions

        document = to_document(model, id_field=self._id_field)
        if document[TENANT_PARTITION_FIELD] != tenant_id:
            raise ValueError(
                f"model's tenant_id {document[TENANT_PARTITION_FIELD]!r} does not match the "
                f"tenant_id {tenant_id!r} this write was scoped to — refusing to write a document "
                f"into a partition the caller did not name (FR-032)"
            )
        kwargs: dict[str, Any] = {}
        if etag is not None:
            kwargs["etag"] = etag
            kwargs["match_condition"] = MatchConditions.IfNotModified
        replaced = await self._container.upsert_item(body=document, **kwargs)
        return from_document(self._model_cls, replaced)

    async def read(self, tenant_id: str, item_id: str) -> T | None:
        """Read one document by id from ``tenant_id``'s partition, or ``None`` if absent."""
        try:
            document = await self._container.read_item(item=item_id, partition_key=tenant_id)
        except CosmosResourceNotFoundError:
            return None
        return from_document(self._model_cls, document)

    async def query(
        self,
        tenant_id: str,
        query: str,
        parameters: Sequence[dict[str, Any]] | None = None,
    ) -> AsyncIterator[T]:
        """Run ``query``, scoped to ``tenant_id``'s partition.

        There is no ``enable_cross_partition_query`` parameter on this method, and none is threaded
        through to the underlying call. Passing a concrete ``partition_key`` to the Cosmos SDK is
        itself what scopes the query — a caller of this method cannot ask for cross-partition
        behaviour because the method gives them no way to.
        """
        items = self._container.query_items(
            query=query, parameters=list(parameters or []), partition_key=tenant_id
        )
        async for document in items:
            yield from_document(self._model_cls, document)


class TenantRegistryContainerLike(Protocol):
    """The subset of ``azure.cosmos.aio.ContainerProxy`` :class:`TenantRegistry` depends on —
    deliberately a separate protocol from :class:`ContainerLike`, the same reasoning
    ``groundwork_shared.queue.subscription_lease.LeaseContainerLike`` gives for not reusing
    :class:`ContainerLike`: this one calls ``query_items`` with no ``partition_key`` at all, which
    is the one thing :class:`TenantScopedRepository` structurally cannot do. Keeping it a distinct
    type means that capability is never accidentally available anywhere ``ContainerLike`` is.
    """

    def query_items(
        self, query: str, *, parameters: list[dict[str, Any]] | None = None, **kwargs: Any
    ) -> AsyncIterator[Any]: ...


class TenantRegistry:
    """Lists onboarded tenant ids — the one deliberate, narrowly-scoped exception to this module's
    own no-cross-partition-query rule (added 2026-08-01, wiring the orchestrator's
    queue-consumption loop).

    **Why this has to exist.** The queue-consumption loop must find every tenant with queued
    deployments, but ``deployments`` (like every tenant-scoped container, including ``tenants``
    itself) is partitioned by ``/tenantId`` — querying it requires already knowing which tenant to
    scope to. `TenantScopedRepository.query` is deliberately incapable of a cross-partition scan
    (its own docstring: "there is no way to ask for cross-partition behaviour"), so nothing already
    in this codebase can answer "which tenants exist" at all. The original design notes do not
    specify a discovery mechanism either — this was a genuine gap surfaced by, not resolved before,
    wiring
    the queue loop.

    **Why this is safe, not a weakening of FR-032.** This queries the ``tenants`` container only,
    and projects only ``tenantId`` — never `deployments`, `plans`, `approvals`, or any container
    holding actual per-customer deployment data, which is what FR-032's tenant isolation guarantee
    exists to protect. A tenant's own onboarding metadata (display name, consent state, concurrency
    cap) is lower-sensitivity than its deployment history, and knowing *that* a tenant id exists is
    structurally necessary for the orchestrator to serve more than one tenant at all — the
    project's single-engagement scope decision (not included in this release) is explicit that the
    architecture must stay real multi-tenant even while only one tenant is onboarded, so there is
    no smaller-scope alternative that still works. Every subsequent read this registry's callers
    perform (a tenant's own queued deployments, its own plan, its own lease) goes through the
    ordinary tenant-scoped path once the tenant id is in hand — this class only ever answers
    "which partitions exist", never "what is in them".
    """

    def __init__(self, container: TenantRegistryContainerLike) -> None:
        self._container = container

    async def list_tenant_ids(self) -> AsyncIterator[str]:
        items = self._container.query_items("SELECT VALUE c.tenantId FROM c")
        async for tenant_id in items:
            yield str(tenant_id)


class CosmosStateStore:
    """Owns the Cosmos client and hands out tenant-scoped repositories for orchestrator state.

    One instance per process, matching the pattern already established for the control plane's own
    Cosmos client in ``groundwork_controlplane.api.main`` — a single ``CosmosClient`` constructed
    once via workload identity and reused for the process lifetime, closed on shutdown.
    """

    def __init__(self, client: CosmosClient, *, database_name: str = DEFAULT_DATABASE_NAME) -> None:
        self._client = client
        self._database = client.get_database_client(database_name)

    def repository[T: BaseModel](
        self, container_name: str, *, model_cls: type[T], id_field: str
    ) -> TenantScopedRepository[T]:
        container = self._database.get_container_client(container_name)
        return TenantScopedRepository(container, model_cls=model_cls, id_field=id_field)

    def tenant_registry(self, container_name: str = "tenants") -> TenantRegistry:
        """See :class:`TenantRegistry`'s own docstring for why this — and only this — bypasses
        :meth:`repository`'s tenant-scoped guarantee."""
        return TenantRegistry(self._database.get_container_client(container_name))

    async def close(self) -> None:
        await self._client.close()
