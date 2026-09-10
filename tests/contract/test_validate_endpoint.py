"""T029 — ``POST /plans/{planId}/validate`` (FR-015, fail-fast validation).

The property this file exists to prove: an ``UNREACHABLE`` check blocks deployment exactly as a
``FAILED`` one does, and there is no ``skipped`` outcome for a check that could not run. See
``groundwork_contracts.readiness.ValidationStatus``'s own docstring for why the type has no
``SKIPPED`` member at all — this file asserts the API-level consequence of that design.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan
from groundwork_contracts.readiness import (
    AssertionSeverity,
    DesignAreaName,
    ReadinessSummary,
    ValidationResult,
    ValidationStatus,
)
from groundwork_contracts.tenant import ConsentState, CustomerTenant, SubscriptionEntitlement
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.plans import router as plans_router
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.settings import ReadinessSettings

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"


def test_validation_status_has_no_skipped_member() -> None:
    """The fail-fast validation rule, structurally: there is no status a check can report that
    means 'not run but counted as fine.' Every member is either PASSED or blocks."""
    assert {status.value for status in ValidationStatus} == {"passed", "failed", "unreachable"}


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

    def query_items(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - unused
        raise NotImplementedError

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body


class _FakeTokenValidator:
    def __init__(self, caller: AuthenticatedCaller) -> None:
        self._caller = caller

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        if authorization_header != "Bearer good-token":
            raise AuthenticationError("no authorization header")
        return self._caller


class _FakePlanningAgent:
    def __init__(self, plan: DeploymentPlan) -> None:
        self._plan = plan

    async def generate_plan(self, conversation_summary: str) -> DeploymentPlan:
        return self._plan


class _FakeReadinessEngine:
    def __init__(self, summary: ReadinessSummary) -> None:
        self.summary = summary

    async def evaluate(self, context: Any, *, now: datetime) -> ReadinessSummary:
        return self.summary


def _caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _entitled_tenant() -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        consent_state=ConsentState.GRANTED,
        consent_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        subscriptions=(
            SubscriptionEntitlement(
                subscription_id=SUBSCRIPTION_ID, display_name="Test Subscription", may_deploy=True
            ),
        ),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
    )


@pytest.fixture
def blueprint() -> PlatformBlueprint:
    from pathlib import Path

    from groundwork_shared.config.blueprints import load_blueprint

    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


def _build_app(
    *, plan: DeploymentPlan, summary: ReadinessSummary, blueprint: PlatformBlueprint
) -> tuple[FastAPI, _FakeReadinessEngine]:
    app = FastAPI()
    app.include_router(plans_router)
    register_error_handlers(app)

    tenant_container = _FakeContainer()
    tenant = _entitled_tenant()
    seeded = tenant.model_dump(mode="json") | {"id": tenant.tenant_id}
    tenant_container.seed(tenant.tenant_id, tenant.tenant_id, seeded)

    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.planning_agent = _FakePlanningAgent(plan)
    app.state.planning_agents = {blueprint.blueprint_id: app.state.planning_agent}
    app.state.plan_repository = TenantScopedRepository(
        _FakeContainer(), model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    app.state.tenant_repository = TenantScopedRepository(
        tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.blueprints = {blueprint.blueprint_id: blueprint}
    app.state.credential = object()
    engine = _FakeReadinessEngine(summary)
    app.state.readiness_engine = engine
    app.state.readiness_engines = {blueprint.blueprint_id: engine}
    app.state.settings = type(
        "FakeSettings", (), {"readiness": ReadinessSettings(orchestrator_principal_id=REQUESTER_ID)}
    )()

    return app, engine


def _create_plan(client: TestClient) -> dict[str, Any]:
    return client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    ).json()


def _summary(*, status: ValidationStatus, now: datetime) -> ReadinessSummary:
    return ReadinessSummary(
        contract_version="1.0.0",
        results=(
            ValidationResult(
                assertion_id="network.vnet-address-space-available",
                contract_version="1.0.0",
                design_area=DesignAreaName.NETWORK_TOPOLOGY,
                status=status,
                severity=AssertionSeverity.BLOCKING,
                finding="check could not reach the target subscription"
                if status is ValidationStatus.UNREACHABLE
                else "10.42.0.0/16 overlaps an existing VNet",
                remediation=(
                    "Retry once connectivity to the target subscription is restored."
                    if status is ValidationStatus.UNREACHABLE
                    else "Choose a non-overlapping address range."
                ),
                evaluated_at=now,
            ),
        ),
        evaluated_at=now,
    )


def test_unreachable_check_blocks_deployment_exactly_like_a_failed_one(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    passing = _summary(status=ValidationStatus.PASSED, now=now)
    app, engine = _build_app(plan=valid_plan, summary=passing, blueprint=blueprint)
    client = TestClient(app)
    created = _create_plan(client)

    engine.summary = _summary(status=ValidationStatus.UNREACHABLE, now=now)
    response = client.post(
        f"/v1/plans/{created['planId']}/validate", headers={"Authorization": "Bearer good-token"}
    )

    assert response.status_code == 409
    failed = response.json()["failedAssertions"]
    assert len(failed) == 1
    assert failed[0]["assertionId"] == "network.vnet-address-space-available"


def test_passed_check_returns_200(valid_plan: DeploymentPlan, blueprint: PlatformBlueprint) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    passing = _summary(status=ValidationStatus.PASSED, now=now)
    app, _engine = _build_app(plan=valid_plan, summary=passing, blueprint=blueprint)
    client = TestClient(app)
    created = _create_plan(client)

    response = client.post(
        f"/v1/plans/{created['planId']}/validate", headers={"Authorization": "Bearer good-token"}
    )

    assert response.status_code == 200
    results = response.json()
    assert all(r["status"] != "unreachable" and r["status"] != "failed" for r in results)


def test_no_response_ever_reports_a_skipped_status(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    passing = _summary(status=ValidationStatus.PASSED, now=now)
    app, _engine = _build_app(plan=valid_plan, summary=passing, blueprint=blueprint)
    client = TestClient(app)
    created = _create_plan(client)

    response = client.post(
        f"/v1/plans/{created['planId']}/validate", headers={"Authorization": "Bearer good-token"}
    )

    statuses = {r["status"] for r in response.json()}
    assert "skipped" not in statuses
