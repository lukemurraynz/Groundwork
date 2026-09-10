"""Per-subscription execution lease (T069; FR-045b).

FR-045b's invariant — at most one deployment per subscription executes at a time — cannot be
enforced by a single :class:`~groundwork_contracts.deployment.Deployment` document, because it is a
property of *other* documents too. This module is the shared state that makes it true: one lease
document per subscription, in the ``subscription_leases`` container (``infra/modules/cosmos.bicep``,
partition key ``/subscriptionId``, default TTL 3600s so a worker that dies without releasing cannot
deadlock the subscription forever).

**Lives in ``groundwork_shared``, not ``groundwork_controlplane`` (moved here 2026-08-01, discovered
while wiring the orchestrator's queue-consumption loop).** Acquiring the lease is fundamentally the
orchestrator's own job — it is the caller of ``Sequencer.run``, and must hold the lease for the
entire execution, not just at admission time (``api/deployments.py``'s own docstring explains why it
deliberately does *not* acquire one). But `groundwork_orchestrator` must never import
`groundwork_controlplane` (the deterministic-execution boundary, enforced by
`test_import_boundaries.py`), and this module was originally placed under the control plane's
own ``queue/`` package where it started (alongside
``admission.py``, which *is* correctly control-plane-only — admission is a creation-time decision,
never an orchestrator concern). The same reasoning `groundwork_shared.config.blueprints`'s own
docstring already records for the blueprint loader applies here: this is shared state both services
need, so it belongs in the shared package, not duplicated or reached across the one-way boundary.

Partitioned by subscription, not tenant, so this deliberately does not go through
:class:`~groundwork_orchestrator.state.cosmos.TenantScopedRepository` — that type hardcodes the
``/tenantId`` partition key as the FR-032 enforcement point, and reusing it here with a different
partition field would blur exactly the guarantee it exists to keep structural.

Verified live 2026-08-01 against the installed ``azure-cosmos`` SDK: ``ContainerProxy.create_item``
fails atomically with ``CosmosResourceExistsError`` if a document with that id already exists in the
partition (no ``etag`` needed for an "acquire if absent" race), while ``replace_item`` accepts
``etag``/``match_condition`` for an atomic "replace only if unchanged since I read it" — together
these cover both races this module has (acquiring a fresh lease, and taking over an expired one)
with no read-then-write window either one leaves open.
"""

from __future__ import annotations

from collections.abc import Awaitable, Mapping
from datetime import datetime, timedelta
from typing import Any, Protocol

from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from groundwork_contracts.deployment import SubscriptionLease

DEFAULT_LEASE_TTL = timedelta(seconds=3600)


class LeaseHeldError(Exception):
    """Another holder's lease is still valid; the caller did not acquire it."""

    def __init__(self, subscription_id: str, holder: str, expires_at: datetime) -> None:
        self.subscription_id = subscription_id
        self.holder = holder
        self.expires_at = expires_at
        super().__init__(
            f"subscription {subscription_id} lease is held by {holder!r} until "
            f"{expires_at.isoformat()}"
        )


class LeaseContainerLike(Protocol):
    """The subset of ``azure.cosmos.aio.ContainerProxy`` this module depends on.

    A separate protocol from ``groundwork_orchestrator.state.cosmos.ContainerLike`` because this
    module needs ``replace_item`` with optimistic-concurrency parameters that the tenant-scoped
    repository never uses.
    """

    def create_item(self, body: dict[str, Any], **kwargs: Any) -> Awaitable[Mapping[str, Any]]: ...

    def read_item(
        self, item: str, partition_key: Any, **kwargs: Any
    ) -> Awaitable[Mapping[str, Any]]: ...

    def replace_item(
        self,
        item: str,
        body: dict[str, Any],
        *,
        etag: str | None = None,
        match_condition: MatchConditions | None = None,
        **kwargs: Any,
    ) -> Awaitable[Mapping[str, Any]]: ...


def _document(subscription_id: str, lease: SubscriptionLease) -> dict[str, Any]:
    return {
        "id": subscription_id,
        "subscriptionId": subscription_id,
        **lease.model_dump(mode="json"),
    }


def _lease_from_document(document: Mapping[str, Any]) -> SubscriptionLease:
    return SubscriptionLease(holder=document["holder"], expires_at=document["expires_at"])


class SubscriptionLeaseStore:
    """Acquires and releases the one lease document per subscription."""

    def __init__(self, container: LeaseContainerLike) -> None:
        self._container = container

    async def acquire(
        self,
        subscription_id: str,
        *,
        holder: str,
        now: datetime,
        ttl: timedelta = DEFAULT_LEASE_TTL,
    ) -> SubscriptionLease:
        """Acquire the lease for ``subscription_id``, or raise :class:`LeaseHeldError`.

        Two paths, both atomic: if no lease document exists yet, ``create_item`` either succeeds
        (we hold it) or fails because a concurrent caller just created one first — in which case we
        fall through to the second path exactly as if we had found it on the first read. If a lease
        document exists, we take it over only when it has expired, and only via an
        ``etag``-conditioned replace so a peer racing the same take-over cannot also succeed.
        """
        lease = SubscriptionLease(holder=holder, expires_at=now + ttl)
        document = _document(subscription_id, lease)

        try:
            await self._container.create_item(body=document)
            return lease
        except CosmosResourceExistsError:
            pass

        existing_doc = await self._container.read_item(
            item=subscription_id, partition_key=subscription_id
        )
        existing = _lease_from_document(existing_doc)
        if existing.is_valid_at(now):
            raise LeaseHeldError(subscription_id, existing.holder, existing.expires_at)

        try:
            await self._container.replace_item(
                item=subscription_id,
                body=document,
                etag=existing_doc["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError:
            # Lost the take-over race after finding it expired — a peer replaced it first.
            # Re-read to report who actually holds it now, rather than a stale expired holder.
            latest_doc = await self._container.read_item(
                item=subscription_id, partition_key=subscription_id
            )
            latest = _lease_from_document(latest_doc)
            raise LeaseHeldError(subscription_id, latest.holder, latest.expires_at) from None
        return lease

    async def release(self, subscription_id: str, *, holder: str, now: datetime) -> None:
        """Release the lease early, if still held by ``holder``.

        Not required for correctness — the TTL means a lease a worker forgets to release still
        frees itself — but releasing promptly lets a queued deployment for the same subscription
        start immediately instead of waiting out the full TTL. A no-op if the lease is already
        gone, already expired, or held by someone else (nothing to release that was ours).
        """
        try:
            existing_doc = await self._container.read_item(
                item=subscription_id, partition_key=subscription_id
            )
        except CosmosResourceNotFoundError:
            return

        existing = _lease_from_document(existing_doc)
        if existing.holder != holder:
            return

        released = SubscriptionLease(holder=holder, expires_at=now)
        try:
            await self._container.replace_item(
                item=subscription_id,
                body=_document(subscription_id, released),
                etag=existing_doc["_etag"],
                match_condition=MatchConditions.IfNotModified,
            )
        except CosmosAccessConditionFailedError:
            # Someone else already took over or released it first; nothing left to do.
            return
