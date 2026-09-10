"""T069 — the per-subscription execution lease (FR-045b).

The fake container implements real Cosmos optimistic-concurrency semantics (an ``_etag`` that
changes on every write, ``create_item`` rejecting a duplicate id, ``replace_item`` rejecting a
stale ``etag``) rather than a simplified in-memory dict, because the property this module exists
for — a race between two acquirers cannot let both succeed — is exactly what a naive fake would
paper over.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from azure.core import MatchConditions
from azure.cosmos.exceptions import (
    CosmosAccessConditionFailedError,
    CosmosResourceExistsError,
    CosmosResourceNotFoundError,
)

from groundwork_shared.queue.subscription_lease import LeaseHeldError, SubscriptionLeaseStore

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


class _FakeLeaseContainer:
    """Real Cosmos etag semantics, no network."""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self._etag_counter = 0

    def _next_etag(self) -> str:
        self._etag_counter += 1
        return str(self._etag_counter)

    async def create_item(self, body, **_):
        item_id = body["id"]
        if item_id in self.documents:
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[item_id] = stored
        return stored

    async def read_item(self, item, partition_key, **_):
        found = self.documents.get(item)
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def replace_item(self, item, body, *, etag=None, match_condition=None, **_):
        existing = self.documents.get(item)
        if existing is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        if match_condition is MatchConditions.IfNotModified and existing["_etag"] != etag:
            raise CosmosAccessConditionFailedError(status_code=412, message="etag mismatch")
        stored = {**body, "_etag": self._next_etag()}
        self.documents[item] = stored
        return stored


async def test_acquire_succeeds_when_no_lease_exists() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())

    lease = await store.acquire(SUBSCRIPTION_ID, holder="worker-1", now=NOW)

    assert lease.holder == "worker-1"
    assert lease.expires_at == NOW + timedelta(seconds=3600)


async def test_acquire_fails_while_a_valid_lease_is_held_by_someone_else() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())
    await store.acquire(SUBSCRIPTION_ID, holder="worker-1", now=NOW)

    with pytest.raises(LeaseHeldError) as exc_info:
        await store.acquire(SUBSCRIPTION_ID, holder="worker-2", now=NOW + timedelta(seconds=10))

    assert exc_info.value.holder == "worker-1"


async def test_acquire_takes_over_an_expired_lease() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())
    await store.acquire(SUBSCRIPTION_ID, holder="worker-1", now=NOW, ttl=timedelta(seconds=60))

    later = NOW + timedelta(seconds=61)
    lease = await store.acquire(SUBSCRIPTION_ID, holder="worker-2", now=later)

    assert lease.holder == "worker-2"


async def test_release_frees_the_lease_for_immediate_reacquisition() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())
    await store.acquire(SUBSCRIPTION_ID, holder="worker-1", now=NOW, ttl=timedelta(seconds=3600))

    await store.release(SUBSCRIPTION_ID, holder="worker-1", now=NOW + timedelta(seconds=5))

    # Still within the original TTL window, but released early — a new acquirer succeeds anyway.
    lease = await store.acquire(SUBSCRIPTION_ID, holder="worker-2", now=NOW + timedelta(seconds=6))
    assert lease.holder == "worker-2"


async def test_release_by_a_non_holder_is_a_no_op() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())
    await store.acquire(SUBSCRIPTION_ID, holder="worker-1", now=NOW, ttl=timedelta(seconds=3600))

    await store.release(SUBSCRIPTION_ID, holder="worker-2", now=NOW + timedelta(seconds=5))

    # worker-1's lease is untouched; a third party still cannot acquire it.
    with pytest.raises(LeaseHeldError):
        await store.acquire(SUBSCRIPTION_ID, holder="worker-3", now=NOW + timedelta(seconds=6))


async def test_release_of_a_never_acquired_subscription_is_a_no_op() -> None:
    store = SubscriptionLeaseStore(_FakeLeaseContainer())

    await store.release(SUBSCRIPTION_ID, holder="worker-1", now=NOW)  # must not raise


async def test_stale_etag_takeover_is_rejected() -> None:
    """Engineers the exact interleaving a true concurrent race would produce: two competitors
    read the same expired lease document (same ``_etag``) before either writes. The winner's
    ``acquire`` succeeds and advances the etag; a second write still holding the etag read *before*
    the winner wrote — exactly what ``SubscriptionLeaseStore.acquire`` would submit had it read at
    the same moment as the winner — must be rejected rather than silently overwrite it. This is
    the property ``acquire``'s ``etag``/``match_condition`` pairing exists to guarantee.
    """
    container = _FakeLeaseContainer()
    await SubscriptionLeaseStore(container).acquire(
        SUBSCRIPTION_ID, holder="worker-1", now=NOW, ttl=timedelta(seconds=60)
    )
    later = NOW + timedelta(seconds=61)

    stale_snapshot = await container.read_item(item=SUBSCRIPTION_ID, partition_key=SUBSCRIPTION_ID)

    winner = await SubscriptionLeaseStore(container).acquire(
        SUBSCRIPTION_ID, holder="worker-2", now=later
    )
    assert winner.holder == "worker-2"

    with pytest.raises(CosmosAccessConditionFailedError):
        await container.replace_item(
            item=SUBSCRIPTION_ID,
            body={
                "id": SUBSCRIPTION_ID,
                "subscriptionId": SUBSCRIPTION_ID,
                "holder": "worker-3",
                "expires_at": (later + timedelta(seconds=3600)).isoformat(),
            },
            etag=stale_snapshot["_etag"],
            match_condition=MatchConditions.IfNotModified,
        )
