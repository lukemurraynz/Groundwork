"""T074 — what-if capture (``engine/preview.py``), unit-tested with ``httpx.MockTransport``.

The same pattern ``tests/unit/test_fabric_stage.py`` uses for its raw-httpx REST calls: mock
the transport layer, not the module under test, so the real polling, error handling, and blob
storage logic is exercised.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from groundwork_contracts.deployment import (
    AuthorityChain,
    Deployment,
    DeploymentStatus,
    SubscriptionLease,
)
from groundwork_contracts.plan import (
    ApprovedRegion,
    DeploymentPlan,
    Environment,
    FabricCapacitySku,
)
from groundwork_orchestrator.engine.preview import (
    WhatIfCapture,
    WhatIfCaptureError,
    WhatIfPreviewStore,
)
from tests.conftest import APPROVAL_ID, SUBSCRIPTION_ID, TENANT_ID

DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
PLAN_HASH = "sha256:" + "a" * 64


def _deployment() -> Deployment:
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 8, 1, tzinfo=UTC)
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id="77777777-7777-7777-7777-777777777777",
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        status=DeploymentStatus.EXECUTING,
        started_at=now,
        lease=SubscriptionLease(holder=DEPLOYMENT_ID, expires_at=now + timedelta(hours=1)),
    )


def _plan() -> DeploymentPlan:
    from datetime import UTC, datetime

    from groundwork_contracts.plan import (
        CostEstimateRef,
        PlanResource,
        RiskAssessment,
        RiskSeverity,
    )

    return DeploymentPlan(
        blueprint_id="standard-production-fabric",
        blueprint_version="1.0.0",
        subscription_id=SUBSCRIPTION_ID,
        region=ApprovedRegion.AUSTRALIA_EAST,
        environment=Environment.PRODUCTION,
        fabric_capacity_sku=FabricCapacitySku.F2,
        resource_set=(
            PlanResource(
                resource_type="Microsoft.Resources/resourceGroups", logical_name="test-rg"
            ),
        ),
        estimated_duration_minutes=45,
        cost_estimate=CostEstimateRef(
            monthly_total=100.0,
            uncertainty_lower_pct=10.0,
            uncertainty_upper_pct=20.0,
            basis="test",
            computed_at=datetime(2026, 8, 1, tzinfo=UTC),
        ),
        risk_assessment=RiskAssessment(severity=RiskSeverity.LOW),
    )


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: object) -> object:
        class _FakeToken:
            token = "fake-token"  # noqa: S105

        return _FakeToken()


class _FakeStore:
    """Records the last stored payload so tests can inspect it."""

    def __init__(self) -> None:
        self.stored: dict[str, Any] | None = None
        self.last_deployment_id: str | None = None

    async def store(self, *, deployment_id: str, payload: dict[str, Any]) -> str:
        self.stored = payload
        self.last_deployment_id = deployment_id
        return f"https://example.invalid/previews/{deployment_id}.json"


def _mock_transport(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


# ---------------------------------------------------------------------------
# What-if response faking helpers
# ---------------------------------------------------------------------------

_WHAT_IF_URL = (
    "/subscriptions/33333333-3333-3333-3333-333333333333/providers/"
    "Microsoft.Resources/deployments/whatif-groundwork-33333333/whatIf"
    "?api-version=2025-04-01"
)

_OPERATION_URL = "https://management.azure.com/subscriptions/33333333-3333-3333-3333-333333333333/operationresults/op-001"


def _make_sync_200(request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"status": "Succeeded", "properties": {"changes": []}})


def _make_202_then_200(request: httpx.Request) -> httpx.Response:
    if request.method == "POST":
        return httpx.Response(
            202,
            headers={"Location": _OPERATION_URL, "Retry-After": "1"},
        )
    return httpx.Response(200, json={"status": "Succeeded"})


def _make_202_no_location(request: httpx.Request) -> httpx.Response:
    return httpx.Response(202, headers={})


def _make_always_202(request: httpx.Request) -> httpx.Response:
    return httpx.Response(202, headers={"Location": _OPERATION_URL})


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_synchronous_200_response() -> None:
    """A what-if that completes immediately (200 on the initial POST)."""
    store = _FakeStore()
    capture = WhatIfCapture(
        store=store,
        http_client=httpx.AsyncClient(transport=_mock_transport(_make_sync_200)),
        poll_interval_seconds=0.01,
        max_poll_attempts=3,
    )
    try:
        url = await capture.capture(
            deployment=_deployment(), plan=_plan(), credential=_FakeCredential()
        )
        assert url == f"https://example.invalid/previews/{DEPLOYMENT_ID}.json"
        assert store.stored is not None
        assert store.stored["deploymentId"] == DEPLOYMENT_ID
    finally:
        await capture.aclose()


@pytest.mark.asyncio
async def test_202_poll_to_completion() -> None:
    """A what-if that returns 202, then the poll URL returns 200."""
    store = _FakeStore()
    capture = WhatIfCapture(
        store=store,
        http_client=httpx.AsyncClient(transport=_mock_transport(_make_202_then_200)),
        poll_interval_seconds=0.01,
        max_poll_attempts=60,
    )
    try:
        _captured = await capture.capture(
            deployment=_deployment(), plan=_plan(), credential=_FakeCredential()
        )
        assert store.stored is not None
        assert store.stored["capturedResult"]["status"] == "Succeeded"
    finally:
        await capture.aclose()


@pytest.mark.asyncio
async def test_202_with_no_location_header_raises() -> None:
    """A 202 with no Location header to poll is a hard error."""
    store = _FakeStore()
    capture = WhatIfCapture(
        store=store,
        http_client=httpx.AsyncClient(transport=_mock_transport(_make_202_no_location)),
        poll_interval_seconds=0.01,
        max_poll_attempts=3,
    )
    try:
        with pytest.raises(WhatIfCaptureError, match="no Location header"):
            await capture.capture(
                deployment=_deployment(), plan=_plan(), credential=_FakeCredential()
            )
    finally:
        await capture.aclose()


@pytest.mark.asyncio
async def test_poll_budget_exhaustion_raises() -> None:
    """When the poll URL keeps returning 202 past the budget, give up."""
    store = _FakeStore()
    capture = WhatIfCapture(
        store=store,
        http_client=httpx.AsyncClient(transport=_mock_transport(_make_always_202)),
        poll_interval_seconds=0.01,
        max_poll_attempts=3,
    )
    try:
        with pytest.raises(WhatIfCaptureError, match="poll budget"):
            await capture.capture(
                deployment=_deployment(), plan=_plan(), credential=_FakeCredential()
            )
    finally:
        await capture.aclose()


@pytest.mark.asyncio
async def test_stored_payload_includes_deployment_and_subscription_ids() -> None:
    """The blob payload carries the ids needed to correlate it back to a deployment."""
    store = _FakeStore()
    capture = WhatIfCapture(
        store=store,
        http_client=httpx.AsyncClient(transport=_mock_transport(_make_sync_200)),
        poll_interval_seconds=0.01,
        max_poll_attempts=3,
    )
    try:
        await capture.capture(deployment=_deployment(), plan=_plan(), credential=_FakeCredential())
        assert store.stored is not None
        assert store.stored["deploymentId"] == DEPLOYMENT_ID
        assert store.stored["subscriptionId"] == SUBSCRIPTION_ID
        assert store.stored["blueprintId"] == "standard-production-fabric"
        assert "capturedResult" in store.stored
    finally:
        await capture.aclose()


# ---------------------------------------------------------------------------
# WhatIfPreviewStore.store() itself — found live 2026-08-24 that nothing exercised the real
# blob-client logic (every test above fakes the whole store via _FakeStore), which is exactly
# why two real bugs here reached a live Storage account before anything caught them: (a)
# azure.storage.blob.aio's upload_blob() returns a plain dict, never an object with .url, and
# (b) the `previews` container's time-based immutability policy means overwrite=True cannot
# actually replace a blob a prior (crashing) attempt already wrote — a retry needs check-before-
# write, not a second upload attempt.
# ---------------------------------------------------------------------------

DEPLOYMENT_ID_FOR_STORE = "88888888-8888-8888-8888-888888888888"


class _FakeBlobClient:
    def __init__(self, *, url: str, already_exists: bool = False) -> None:
        self.url = url
        self._exists = already_exists
        self.upload_calls = 0

    async def exists(self, **kwargs: Any) -> bool:
        return self._exists

    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]:
        self.upload_calls += 1
        self._exists = True
        # The real SDK's actual return shape — a plain dict of blob properties, never an object
        # with a .url attribute. Returning that shape here is what makes this test able to catch
        # the AttributeError a `.url`-typed fake could never surface.
        return {"etag": '"fake-etag"', "last_modified": "2026-08-24T00:00:00Z"}


class _FakeContainerClient:
    def __init__(self, blob_client: _FakeBlobClient) -> None:
        self._blob_client = blob_client
        self.get_blob_client_calls: list[str] = []

    def get_blob_client(self, blob: str) -> _FakeBlobClient:
        self.get_blob_client_calls.append(blob)
        return self._blob_client


async def test_store_uploads_and_returns_the_deterministic_url_when_absent() -> None:
    blob = _FakeBlobClient(url="https://example.invalid/previews/whatif.json")
    container = _FakeContainerClient(blob)
    store = WhatIfPreviewStore(container)

    url = await store.store(deployment_id=DEPLOYMENT_ID_FOR_STORE, payload={"a": 1})

    assert url == blob.url
    assert blob.upload_calls == 1
    assert container.get_blob_client_calls == [f"{DEPLOYMENT_ID_FOR_STORE}.json"]


async def test_store_is_idempotent_against_an_already_written_immutable_blob() -> None:
    """The real-world case this fix exists for: a prior attempt already wrote the blob (an
    immutable container makes overwrite=True unable to replace it), so a retry must return the
    existing blob's URL without attempting to upload again."""
    blob = _FakeBlobClient(url="https://example.invalid/previews/whatif.json", already_exists=True)
    container = _FakeContainerClient(blob)
    store = WhatIfPreviewStore(container)

    url = await store.store(deployment_id=DEPLOYMENT_ID_FOR_STORE, payload={"a": 1})

    assert url == blob.url
    assert blob.upload_calls == 0
