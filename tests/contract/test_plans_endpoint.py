"""T049 — the plans API end to end, against fakes at the same boundary every other test in this
session fakes at: the real Azure SDK client, the real Foundry agent call, and the real ARM checks
inside the readiness engine. Everything above that boundary — auth, entitlement, tenant-partition
scoping, the schema-boundary adapter, plan sealing, RFC 9457 error mapping — runs for real.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
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
from groundwork_shared.validation.checks.network import DEFAULT_VNET_ADDRESS_SPACE

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"


class _FakeContainer:
    """In-memory stand-in for ``azure.cosmos.aio.ContainerProxy`` (``ContainerLike``).

    Backs a real :class:`TenantScopedRepository` — the repository's own tenant-scoping and
    (de)serialisation logic runs unfaked; only the Cosmos wire call is replaced, matching how
    ``groundwork_orchestrator.state.cosmos``'s own tests fake this boundary.
    """

    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        key = (body["tenantId"], body["id"])
        self._items[key] = body
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        key = (body["tenantId"], body["id"])
        self._items[key] = body
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        found = self._items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    def query_items(self, *_: Any, **__: Any) -> Any:  # pragma: no cover - unused by these tests
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
        self.last_conversation_summary: str | None = None

    async def generate_plan(self, conversation_summary: str) -> DeploymentPlan:
        self.last_conversation_summary = conversation_summary
        return self._plan


class _FakeReadinessEngine:
    def __init__(self, summary: ReadinessSummary) -> None:
        self.summary = summary
        self.evaluated_contexts: list[Any] = []

    async def evaluate(self, context: Any, *, now: datetime) -> ReadinessSummary:
        self.evaluated_contexts.append(context)
        return self.summary


def _deployable_summary(*, now: datetime) -> ReadinessSummary:
    return ReadinessSummary(
        contract_version="1.0.0",
        results=(
            ValidationResult(
                assertion_id="tenant.subscription-reachable",
                contract_version="1.0.0",
                design_area=DesignAreaName.BILLING_AND_TENANT,
                status=ValidationStatus.PASSED,
                severity=AssertionSeverity.BLOCKING,
                finding="subscription is reachable and Enabled",
                evaluated_at=now,
            ),
        ),
        evaluated_at=now,
    )


def _not_deployable_summary(*, now: datetime) -> ReadinessSummary:
    return ReadinessSummary(
        contract_version="1.0.0",
        results=(
            ValidationResult(
                assertion_id="network.vnet-address-space-available",
                contract_version="1.0.0",
                design_area=DesignAreaName.NETWORK_TOPOLOGY,
                status=ValidationStatus.FAILED,
                severity=AssertionSeverity.BLOCKING,
                finding=f"{DEFAULT_VNET_ADDRESS_SPACE} overlaps an existing VNet",
                remediation="Choose a non-overlapping address range.",
                evaluated_at=now,
            ),
        ),
        evaluated_at=now,
    )


def _caller(*, roles: Sequence[CallerRole] = (CallerRole.REQUESTER,)) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset(roles),
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


def _build_app(
    *,
    plan: DeploymentPlan,
    summary: ReadinessSummary,
    tenant: CustomerTenant | None,
    blueprint: PlatformBlueprint,
    caller: AuthenticatedCaller | None = None,
) -> tuple[FastAPI, _FakePlanningAgent, _FakeReadinessEngine]:
    app = FastAPI()
    app.include_router(plans_router)
    register_error_handlers(app)

    plan_container = _FakeContainer()
    tenant_container = _FakeContainer()
    if tenant is not None:
        seeded = tenant.model_dump(mode="json") | {"id": tenant.tenant_id}
        tenant_container.seed(tenant.tenant_id, tenant.tenant_id, seeded)

    app.state.token_validator = _FakeTokenValidator(caller or _caller())
    planning_agent = _FakePlanningAgent(plan)
    app.state.planning_agent = planning_agent
    app.state.planning_agents = {blueprint.blueprint_id: planning_agent}
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    app.state.tenant_repository = TenantScopedRepository(
        tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.blueprints = {blueprint.blueprint_id: blueprint}
    app.state.credential = object()
    readiness_engine = _FakeReadinessEngine(summary)
    app.state.readiness_engine = readiness_engine
    app.state.readiness_engines = {blueprint.blueprint_id: readiness_engine}
    app.state.settings = type(
        "FakeSettings", (), {"readiness": ReadinessSettings(orchestrator_principal_id=REQUESTER_ID)}
    )()

    return app, planning_agent, readiness_engine


@pytest.fixture
def blueprint(valid_plan: DeploymentPlan) -> PlatformBlueprint:
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


def test_create_plan_returns_201_with_validation_summary(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
            "environment": "production",
        },
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["planHash"] == valid_plan.content_hash()
    assert body["deployable"] is True
    assert "validationSummary" in body
    # FR-013d: the F2 fixture is below F64, so the response must carry the disclosure statement
    # itself (a flag alone is not "having been shown"), and the flag with it.
    assert body["requiresPowerBiViewerLicensing"] is True
    assert "Pro" in body["licensingDisclosure"]
    assert "PPU" in body["licensingDisclosure"]
    assert agent.last_conversation_summary is not None
    assert SUBSCRIPTION_ID in agent.last_conversation_summary


def test_create_plan_rejects_tenant_id_in_body(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
            "tenantId": "22222222-2222-2222-2222-222222222222",
        },
    )

    assert response.status_code == 400
    assert response.headers["content-type"] == "application/problem+json"


def test_create_plan_without_token_is_401(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    )

    assert response.status_code == 401


def test_create_plan_unentitled_subscription_is_403(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    tenant = _entitled_tenant().model_copy(update={"subscriptions": ()})
    app, _agent, _engine = _build_app(
        plan=valid_plan, summary=_deployable_summary(now=now), tenant=tenant, blueprint=blueprint
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    )

    assert response.status_code == 403


def test_create_plan_unapproved_region_is_409(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiasoutheast",
        },
    )

    assert response.status_code == 409


def test_create_plan_ungranted_consent_is_403(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    tenant = _entitled_tenant().model_copy(update={"consent_state": ConsentState.REVOKED})
    app, _agent, _engine = _build_app(
        plan=valid_plan, summary=_deployable_summary(now=now), tenant=tenant, blueprint=blueprint
    )
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    )

    assert response.status_code == 403


def test_get_plan_is_scoped_to_the_callers_tenant(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    created = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    ).json()

    found = client.get(
        f"/v1/plans/{created['planId']}", headers={"Authorization": "Bearer good-token"}
    )
    assert found.status_code == 200
    assert found.json()["planHash"] == created["planHash"]

    missing = client.get(
        "/v1/plans/sha256:" + "0" * 64, headers={"Authorization": "Bearer good-token"}
    )
    assert missing.status_code == 404


def test_validate_plan_returns_200_when_deployable(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, _engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    created = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    ).json()

    response = client.post(
        f"/v1/plans/{created['planId']}/validate", headers={"Authorization": "Bearer good-token"}
    )
    assert response.status_code == 200
    results = response.json()
    assert results[0]["status"] == "passed"


def test_validate_plan_returns_409_with_failed_assertions_when_not_deployable(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    now = datetime(2026, 7, 31, tzinfo=UTC)
    app, _agent, engine = _build_app(
        plan=valid_plan,
        summary=_deployable_summary(now=now),
        tenant=_entitled_tenant(),
        blueprint=blueprint,
    )
    client = TestClient(app)

    created = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    ).json()

    engine.summary = _not_deployable_summary(now=now)
    response = client.post(
        f"/v1/plans/{created['planId']}/validate", headers={"Authorization": "Bearer good-token"}
    )
    assert response.status_code == 409
    body = response.json()
    assert body["failedAssertions"][0]["assertionId"] == "network.vnet-address-space-available"


def test_create_plan_resolves_runtime_by_blueprint_id(valid_plan: DeploymentPlan) -> None:
    from groundwork_shared.config.blueprints import load_blueprint

    now = datetime(2026, 7, 31, tzinfo=UTC)
    standard = load_blueprint(
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    sandbox = load_blueprint(
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "dev-sandbox"
        / "blueprint.yaml"
    )
    sandbox_plan = DeploymentPlan.model_validate(
        valid_plan.model_copy(
            update={"blueprint_id": sandbox.blueprint_id, "blueprint_version": sandbox.version}
        ).model_dump(mode="json")
    )

    app = FastAPI()
    app.include_router(plans_router)
    register_error_handlers(app)

    plan_container = _FakeContainer()
    tenant_container = _FakeContainer()
    tenant = _entitled_tenant()
    tenant_container.seed(
        tenant.tenant_id,
        tenant.tenant_id,
        tenant.model_dump(mode="json") | {"id": tenant.tenant_id},
    )

    standard_agent = _FakePlanningAgent(valid_plan)
    sandbox_agent = _FakePlanningAgent(sandbox_plan)
    standard_engine = _FakeReadinessEngine(_deployable_summary(now=now))
    sandbox_engine = _FakeReadinessEngine(_not_deployable_summary(now=now))

    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    app.state.tenant_repository = TenantScopedRepository(
        tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.blueprints = {
        standard.blueprint_id: standard,
        sandbox.blueprint_id: sandbox,
    }
    app.state.planning_agents = {
        standard.blueprint_id: standard_agent,
        sandbox.blueprint_id: sandbox_agent,
    }
    app.state.readiness_engines = {
        standard.blueprint_id: standard_engine,
        sandbox.blueprint_id: sandbox_engine,
    }
    app.state.credential = object()
    app.state.settings = type(
        "FakeSettings", (), {"readiness": ReadinessSettings(orchestrator_principal_id=REQUESTER_ID)}
    )()

    client = TestClient(app)
    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer good-token"},
        json={
            "blueprintId": "dev-sandbox",
            "blueprintVersion": sandbox.version,
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
            "environment": "non-production",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["blueprintId"] == "dev-sandbox"
    assert response.json()["deployable"] is False
    assert sandbox_agent.last_conversation_summary is not None
    assert standard_agent.last_conversation_summary is None
    assert len(sandbox_engine.evaluated_contexts) == 1
    assert len(standard_engine.evaluated_contexts) == 0
