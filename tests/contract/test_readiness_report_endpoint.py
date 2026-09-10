"""Contract tests for the operator-facing readiness report route."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.blueprint import DesignAreaName
from groundwork_contracts.readiness import (
    AssertionSeverity,
    DriftSummary,
    DriftVerdict,
    ReadinessSummary,
    ValidationResult,
    ValidationStatus,
)
from groundwork_contracts.tenant import CustomerTenant, SubscriptionEntitlement
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.readiness_reports import router as readiness_reports_router

pytestmark = pytest.mark.contract

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "22222222-2222-2222-2222-222222222222"
SECOND_SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
OPERATOR_ID = "77777777-7777-7777-7777-777777777777"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"


class _FakeTenantRepository:
    def __init__(self, tenant: CustomerTenant) -> None:
        self._tenant = tenant

    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None:
        if tenant_id == self._tenant.tenant_id and item_id == self._tenant.tenant_id:
            return self._tenant
        return None


class _FakeDriftRepository:
    def __init__(self, summary: DriftSummary | None) -> None:
        self._summary = summary

    async def read(self, tenant_id: str, item_id: str) -> DriftSummary | None:
        if self._summary is None:
            return None
        if tenant_id == self._summary.tenant_id and item_id == self._summary.subscription_id:
            return self._summary
        return None


class _FakeTokenValidator:
    def __init__(self, callers: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        token = (authorization_header or "").removeprefix("Bearer ")
        caller = self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown or missing token")
        return caller


def _caller(object_id: str, *, roles: tuple[CallerRole, ...]) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=object_id,
        tenant_id=TENANT_ID,
        display_name=f"Test Caller {object_id[:8]}",
        roles=frozenset(roles),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _tenant() -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        subscriptions=(
            SubscriptionEntitlement(
                subscription_id=SUBSCRIPTION_ID,
                display_name="Primary",
                may_deploy=True,
            ),
            SubscriptionEntitlement(
                subscription_id=SECOND_SUBSCRIPTION_ID,
                display_name="Secondary",
                may_deploy=True,
            ),
        ),
    )


def _drift_summary() -> DriftSummary:
    return DriftSummary(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        region="australiaeast",
        verdict=DriftVerdict.DRIFTED,
        summary=ReadinessSummary(
            contract_version="1.0.0",
            evaluated_at=NOW,
            results=(
                ValidationResult(
                    assertion_id="network.address-space",
                    contract_version="1.0.0",
                    design_area=DesignAreaName.NETWORK_TOPOLOGY,
                    status=ValidationStatus.FAILED,
                    severity=AssertionSeverity.BLOCKING,
                    finding="Address space overlaps an existing VNet.",
                    remediation="Choose a non-overlapping address range.",
                    evaluated_at=NOW,
                ),
            ),
        ),
    )


def _build_app(*, include_drift_summary: bool, callers: dict[str, AuthenticatedCaller]) -> FastAPI:
    app = FastAPI()
    app.include_router(readiness_reports_router)
    register_error_handlers(app)

    tenant = _tenant()
    app.state.tenant_repository = _FakeTenantRepository(tenant)
    app.state.drift_summary_repository = _FakeDriftRepository(
        _drift_summary() if include_drift_summary else None
    )
    app.state.now_fn = lambda: NOW
    app.state.token_validator = _FakeTokenValidator(callers)
    return app


def test_readiness_report_requires_authentication() -> None:
    app = _build_app(
        include_drift_summary=True,
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
    )
    client = TestClient(app)

    response = client.get(f"/v1/tenants/{TENANT_ID}/onboarding/readiness-report")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_readiness_report_requires_operator_role() -> None:
    app = _build_app(
        include_drift_summary=True,
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))},
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/readiness-report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"


def test_readiness_report_returns_json_metadata_by_default() -> None:
    app = _build_app(
        include_drift_summary=True,
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/readiness-report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "verdict": "drifted",
        "evaluatedAt": NOW.isoformat(),
        "blockingCount": 1,
    }


def test_readiness_report_accepts_html() -> None:
    app = _build_app(
        include_drift_summary=True,
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/readiness-report",
        headers={"Authorization": "Bearer good-token", "Accept": "text/html"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "<h1>Groundwork readiness assessment report</h1>" in response.text
    assert "<table>" in response.text
    assert SUBSCRIPTION_ID in response.text


def test_readiness_report_missing_summary_is_problem_404() -> None:
    app = _build_app(
        include_drift_summary=False,
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/readiness-report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["detail"] == "readiness report not yet generated"
