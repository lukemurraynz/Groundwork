"""T055 — ``POST /plans/{planId}/approvals`` (the gated-approval rule).

Covers exactly what the original task plan names for this contract test: hash mismatch, cost
mismatch, voice-only rejection, expiry, and same-identity second approval — plus the two happy
paths
(single approval below threshold, the two-step flow above it) since a contract test that only
proves the failure modes without ever proving the success shape is incomplete.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.approval import Approval, PendingApproval
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan
from groundwork_controlplane.api.approvals import router as approvals_router
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.approval.plan_identity import seal_plan
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.settings import GovernanceSettings
from groundwork_shared.costing.estimator import compose_estimate, fabric_capacity_line
from groundwork_shared.costing.retail_prices import RetailPricesClient

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
APPROVER_ID = "55555555-5555-5555-5555-555555555555"
SECOND_APPROVER_ID = "66666666-6666-6666-6666-666666666666"
NOW = datetime(2026, 7, 31, tzinfo=UTC)


class _FakeContainer:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        found = self._items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def query_items(
        self, query: str, *, parameters: Any = None, partition_key: Any = None, **_: Any
    ):
        wants_second = "NOT IS_DEFINED" not in query
        for (tenant, _item_id), doc in self._items.items():
            if tenant != partition_key:
                continue
            for param in parameters or []:
                if param["name"] == "@plan_hash" and doc.get("plan_hash") != param["value"]:
                    break
            else:
                has_second = "second_approval" in doc
                if has_second == wants_second:
                    yield doc

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body


class _FakeTokenValidator:
    def __init__(self, callers: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        token = (authorization_header or "").removeprefix("Bearer ")
        caller = self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown token")
        return caller


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


class _FakeArtefactStore:
    """Duck-types :class:`ApprovalArtefactStore`'s public surface without touching real blob
    storage — same reasoning as every other fake in this test suite."""

    def __init__(self) -> None:
        self.uploaded: dict[str, dict[str, Any]] = {}

    def blob_url_for(self, approval_id: str) -> str:
        return f"https://example.invalid/approvals/{approval_id}.json"

    async def store(self, *, approval_id: str, payload: dict[str, Any]) -> str:
        self.uploaded[approval_id] = payload
        return self.blob_url_for(approval_id)


def _caller(
    object_id: str,
    *,
    roles: tuple[CallerRole, ...] = (CallerRole.APPROVER,),
    authentication_methods: frozenset[str] = frozenset(),
    token_issued_at: datetime | None = None,
) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=object_id,
        tenant_id=TENANT_ID,
        display_name=f"Test Approver {object_id[:8]}",
        roles=frozenset(roles),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        authentication_methods=authentication_methods,
        token_issued_at=token_issued_at,
    )


def _fabric_price_client(*, price_per_cu_hour: float = 0.304326) -> RetailPricesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        item = {
            "meterName": "Data Warehouse Capacity Usage CU",
            "productName": "Fabric Capacity",
            "skuName": "m",
            "serviceName": "Microsoft Fabric",
            "armRegionName": "australiaeast",
            "retailPrice": price_per_cu_hour,
            "unitOfMeasure": "1 Hour",
            "currencyCode": "AUD",
            "type": "Consumption",
        }
        return httpx.Response(200, json={"Items": [item], "NextPageLink": None, "Count": 1})

    return RetailPricesClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _expected_monthly_total(
    retail_prices_client: RetailPricesClient, sku: Any, region: str
) -> float:
    fabric_line = await fabric_capacity_line(retail_prices_client, sku, region)
    return compose_estimate(fabric_line, now=NOW).monthly_total


def _build_app(
    *,
    sealed_plan: SealedDeploymentPlan,
    threshold_aud: float,
    retail_prices_client: RetailPricesClient,
    callers: dict[str, AuthenticatedCaller],
    require_step_up_approval: bool = False,
) -> tuple[FastAPI, _FakeArtefactStore]:
    app = FastAPI()
    app.include_router(approvals_router)
    register_error_handlers(app)

    plan_container = _FakeContainer()
    plan_doc = {
        **sealed_plan.model_dump(mode="json"),
        "id": sealed_plan.plan_hash,
        "tenantId": sealed_plan.tenant_id,
    }
    plan_container.seed(sealed_plan.tenant_id, sealed_plan.plan_hash, plan_doc)

    app.state.token_validator = _FakeTokenValidator(callers)
    app.state.credential = _FakeCredential()
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    approvals_container = _FakeContainer()
    app.state.approval_repository = TenantScopedRepository(
        approvals_container, model_cls=Approval, id_field="approval_id"
    )
    app.state.pending_approval_repository = TenantScopedRepository(
        approvals_container, model_cls=PendingApproval, id_field="approval_id"
    )
    app.state.retail_prices_client = retail_prices_client
    # Deterministic clock: without this the route falls back to a real datetime.now(UTC), and a
    # plan sealed against the fixed NOW above will eventually — for real, not hypothetically —
    # read as expired once enough wall-clock time actually passes. See _now()'s own docstring in
    # api/approvals.py.
    app.state.now_fn = lambda: NOW
    artefact_store = _FakeArtefactStore()
    app.state.approval_artefact_store = artefact_store
    app.state.settings = type(
        "FakeSettings",
        (),
        {
            "governance": GovernanceSettings(
                approval_threshold_aud=threshold_aud,
                approver_role="Groundwork.Approver",
                default_tenant_concurrency_cap=3,
                require_step_up_approval=require_step_up_approval,
            )
        },
    )()

    return app, artefact_store


def _sealed_plan(valid_plan: DeploymentPlan, *, now: datetime = NOW) -> SealedDeploymentPlan:
    return seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )


@pytest.fixture
def retail_prices_client() -> RetailPricesClient:
    return _fabric_price_client()


async def test_approval_below_threshold_completes_immediately(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, artefact_store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["secondApprovalRequired"] is False
    assert body["isSelfApproval"] is True
    assert artefact_store.uploaded  # evidence was written


async def test_approval_above_threshold_requires_second_distinct_approver(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _artefact_store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total - 1,
        retail_prices_client=retail_prices_client,
        callers={
            "first-token": _caller(APPROVER_ID),
            "second-token": _caller(SECOND_APPROVER_ID),
        },
    )
    client = TestClient(app)
    request_body = {
        "planHash": sealed.plan_hash,
        "acknowledgedCostAud": expected_total,
        "acknowledgedPowerBiViewerLicensing": True,
        "channel": "teams",
    }

    first = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer first-token"},
        json=request_body,
    )
    assert first.status_code == 201
    assert first.json()["secondApprovalRequired"] is True

    same_identity_again = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer first-token"},
        json=request_body,
    )
    assert same_identity_again.status_code == 409

    second = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer second-token"},
        json=request_body,
    )
    assert second.status_code == 201
    assert second.json()["secondApprovalRequired"] is False


async def test_plan_hash_mismatch_is_409(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": "sha256:" + "0" * 64,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"


async def test_cost_mismatch_is_409(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total * 5,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 409


async def test_voice_channel_is_accepted(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """ADR-0011 (2026-08-02) — voice may now authorise an irreversible action."""
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "voice",
        },
    )

    assert response.status_code == 201


async def test_expired_plan_is_410(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    long_ago = NOW - timedelta(hours=48)
    sealed = _sealed_plan(valid_plan, now=long_ago)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 410


async def test_approval_without_approver_role_is_403(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"requester-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer requester-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 403


async def test_below_f64_approval_without_licensing_acknowledgement_is_409(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """FR-013d's own guarantee, negatively: a below-F64 plan (the F2 fixture) whose approval
    request never acknowledged the Power BI viewer-licensing disclosure is refused before any
    approval record or artefact exists."""
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, artefact_store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "channel": "teams",
        },
    )

    assert response.status_code == 409, response.text
    assert response.json()["type"].endswith("licensing-disclosure-not-acknowledged")
    assert not artefact_store.uploaded  # nothing was recorded


async def test_f64_and_above_needs_no_licensing_acknowledgement(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The gate is scoped to below F64 exactly: an F64 plan approves without the flag, and its
    recorded estimate still notes the disclosure did not apply to this SKU."""
    from groundwork_contracts.blueprint import FabricCapacitySku

    f64_plan = valid_plan.model_copy(update={"fabric_capacity_sku": FabricCapacitySku.F64})
    sealed = _sealed_plan(f64_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, FabricCapacitySku.F64, valid_plan.region.value
    )
    app, _ = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "channel": "teams",
        },
    )

    assert response.status_code == 201, response.text


@pytest.mark.security
async def test_step_up_approval_accepts_mfa_claim(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={
            "mfa-token": _caller(
                APPROVER_ID,
                authentication_methods=frozenset({"mfa", "rsa"}),
                token_issued_at=NOW - timedelta(hours=1),
            )
        },
        require_step_up_approval=True,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer mfa-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 201, response.text


@pytest.mark.security
async def test_step_up_approval_rejects_stale_non_mfa_token(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={
            "stale-token": _caller(
                APPROVER_ID,
                token_issued_at=NOW - timedelta(minutes=11),
            )
        },
        require_step_up_approval=True,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer stale-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 403, response.text
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["type"].endswith("step-up-authentication-required")


@pytest.mark.security
async def test_step_up_approval_disabled_ignores_token_evidence(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={
            "plain-token": _caller(
                APPROVER_ID,
                token_issued_at=NOW - timedelta(days=1),
            )
        },
        require_step_up_approval=False,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer plain-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == 201, response.text


@pytest.mark.security
async def test_step_up_approval_also_applies_to_second_approver(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _artefact_store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total - 1,
        retail_prices_client=retail_prices_client,
        callers={
            "first-token": _caller(
                APPROVER_ID,
                authentication_methods=frozenset({"mfa"}),
                token_issued_at=NOW - timedelta(hours=1),
            ),
            "second-token": _caller(
                SECOND_APPROVER_ID,
                token_issued_at=NOW - timedelta(minutes=11),
            ),
        },
        require_step_up_approval=True,
    )
    client = TestClient(app)
    request_body = {
        "planHash": sealed.plan_hash,
        "acknowledgedCostAud": expected_total,
        "acknowledgedPowerBiViewerLicensing": True,
        "channel": "teams",
    }

    first = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer first-token"},
        json=request_body,
    )
    assert first.status_code == 201, first.text
    assert first.json()["secondApprovalRequired"] is True

    second = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer second-token"},
        json=request_body,
    )

    assert second.status_code == 403, second.text
    assert second.json()["type"].endswith("step-up-authentication-required")


@pytest.mark.security
@pytest.mark.parametrize(
    ("issued_at", "expected_status"),
    [
        (NOW - timedelta(minutes=9), 201),
        (NOW - timedelta(minutes=11), 403),
    ],
)
async def test_step_up_approval_freshness_boundary(
    valid_plan: DeploymentPlan,
    retail_prices_client: RetailPricesClient,
    issued_at: datetime,
    expected_status: int,
) -> None:
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, _store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"boundary-token": _caller(APPROVER_ID, token_issued_at=issued_at)},
        require_step_up_approval=True,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/plans/{sealed.plan_hash}/approvals",
        headers={"Authorization": "Bearer boundary-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
            "channel": "teams",
        },
    )

    assert response.status_code == expected_status, response.text
