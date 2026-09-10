"""T087 — ``GET /deployments/{deploymentId}/report`` (FR-052, FR-052b, SC-015)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.reports import router as reports_router
from groundwork_orchestrator.state.cosmos import TenantScopedRepository

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


class _FakeContainer:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        found = self._items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body


class _FakeTokenValidator:
    def __init__(self, caller: AuthenticatedCaller) -> None:
        self._caller = caller

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        if authorization_header != "Bearer good-token":
            raise AuthenticationError("no authorization header")
        return self._caller


def _caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER, CallerRole.APPROVER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _report(*, retention_expires_at: datetime) -> DeploymentReport:
    return DeploymentReport(
        report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=ReportOutcome.HALTED,
        stage_summary=(
            StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
            StageSummary(stage_name="infrastructure", status=StageStatus.FAILED, attempts=3),
            StageSummary(stage_name="identity", never_ran=True, attempts=0),
        ),
        resources_created=("proj-groundwork-33333333",),
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=412.50,
        blob_uri="https://example.invalid/reports/dddddddd.json",
        content_hash="sha256:" + "0" * 64,
        generated_at=NOW,
        retention_expires_at=retention_expires_at,
    )


def _build_app(*, report: DeploymentReport | None, now: datetime = NOW) -> FastAPI:
    app = FastAPI()
    app.include_router(reports_router)
    register_error_handlers(app)

    container = _FakeContainer()
    if report is not None:
        doc = report.model_dump(mode="json") | {
            "id": report.deployment_id,
            "tenantId": report.tenant_id,
        }
        container.seed(report.tenant_id, report.deployment_id, doc)
    app.state.report_repository = TenantScopedRepository(
        container, model_cls=DeploymentReport, id_field="deployment_id"
    )
    app.state.now_fn = lambda: now
    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.credential = _FakeCredential()
    return app


def test_report_not_yet_generated_is_404() -> None:
    app = _build_app(report=None)
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 404


def test_report_returns_full_stage_summary_including_never_ran() -> None:
    app = _build_app(report=_report(retention_expires_at=NOW + timedelta(days=365)))
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["outcome"] == "halted"
    stage_names = {s["stageName"]: s for s in body["stageSummary"]}
    assert stage_names["devops_project"]["status"] == "succeeded"
    assert stage_names["devops_project"]["neverRan"] is False
    assert stage_names["infrastructure"]["status"] == "failed"
    assert stage_names["identity"]["neverRan"] is True
    assert stage_names["identity"]["status"] is None
    # SC-015: a failed deployment's report is never absent because it failed.
    assert body["blobUri"] == "https://example.invalid/reports/dddddddd.json"


def test_report_past_retention_is_410_not_404() -> None:
    """FR-052b: an expired report must say so explicitly, not report 404 as if it never existed."""
    app = _build_app(
        report=_report(retention_expires_at=NOW - timedelta(days=1)),
        now=NOW,
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 410


def test_report_right_at_retention_boundary_is_410() -> None:
    app = _build_app(report=_report(retention_expires_at=NOW), now=NOW)
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 410


def test_report_accepts_html_returns_accessible_rendering() -> None:
    """T094/FR-004c: an Accept: text/html request gets the WCAG 2.2 AA rendering, not JSON."""
    app = _build_app(report=_report(retention_expires_at=NOW + timedelta(days=365)))
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token", "Accept": "text/html"},
    )

    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/html")
    assert "<table>" in response.text
    assert DEPLOYMENT_ID in response.text


def test_report_scoped_to_callers_tenant() -> None:
    """A report seeded under a different tenant's partition must not be readable."""
    other_tenant_id = "99999999-9999-9999-9999-999999999999"
    other_tenant_report = _report(retention_expires_at=NOW + timedelta(days=365)).model_copy(
        update={"tenant_id": other_tenant_id}
    )

    app = FastAPI()
    app.include_router(reports_router)
    register_error_handlers(app)
    container = _FakeContainer()
    doc = other_tenant_report.model_dump(mode="json") | {
        "id": other_tenant_report.deployment_id,
        "tenantId": other_tenant_report.tenant_id,
    }
    container.seed(other_tenant_id, other_tenant_report.deployment_id, doc)
    app.state.report_repository = TenantScopedRepository(
        container, model_cls=DeploymentReport, id_field="deployment_id"
    )
    app.state.now_fn = lambda: NOW
    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.credential = _FakeCredential()
    client = TestClient(app)

    response = client.get(
        f"/v1/deployments/{DEPLOYMENT_ID}/report",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 404
