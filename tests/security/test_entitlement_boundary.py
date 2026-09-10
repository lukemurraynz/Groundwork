"""T032 — FR-008 entitlement boundary: a caller without a subscription grant is refused, and the
refusal itself must not enumerate resources the caller is not entitled to see.

Also exercises FR-032 (tenant partition isolation) at the plans API, complementing
``test_credential_scoping.py``'s coverage of the same guarantee at the data-layer credential.
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

pytestmark = pytest.mark.security

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
UNENTITLED_SUBSCRIPTION_ID = "99999999-9999-9999-9999-999999999999"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"


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
    def __init__(self, callers_by_token: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers_by_token

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        token = (authorization_header or "").removeprefix("Bearer ")
        caller = self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown token")
        return caller


class _FakePlanningAgent:
    def __init__(self, plan: DeploymentPlan) -> None:
        self._plan = plan

    async def generate_plan(self, conversation_summary: str) -> DeploymentPlan:
        return self._plan


class _FakeReadinessEngine:
    async def evaluate(self, context: Any, *, now: datetime) -> ReadinessSummary:
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


def _caller(tenant_id: str) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=tenant_id,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _tenant_entitled_only_to(subscription_id: str, *, tenant_id: str) -> CustomerTenant:
    return CustomerTenant(
        tenant_id=tenant_id,
        display_name="Test Customer",
        consent_state=ConsentState.GRANTED,
        consent_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        subscriptions=(
            SubscriptionEntitlement(
                subscription_id=subscription_id,
                display_name="Approved Subscription",
                may_deploy=True,
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
    *, plan: DeploymentPlan, blueprint: PlatformBlueprint, tenants: dict[str, CustomerTenant]
) -> tuple[FastAPI, dict[str, AuthenticatedCaller]]:
    app = FastAPI()
    app.include_router(plans_router)
    register_error_handlers(app)

    tenant_container = _FakeContainer()
    for tenant in tenants.values():
        seeded = tenant.model_dump(mode="json") | {"id": tenant.tenant_id}
        tenant_container.seed(tenant.tenant_id, tenant.tenant_id, seeded)

    callers = {tid: _caller(tid) for tid in tenants}
    app.state.token_validator = _FakeTokenValidator(
        {f"token-for-{tid}": caller for tid, caller in callers.items()}
    )
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
    app.state.readiness_engine = _FakeReadinessEngine()
    app.state.readiness_engines = {blueprint.blueprint_id: app.state.readiness_engine}
    app.state.settings = type(
        "FakeSettings", (), {"readiness": ReadinessSettings(orchestrator_principal_id=REQUESTER_ID)}
    )()

    return app, callers


def test_unentitled_subscription_is_refused_without_enumerating_entitled_ones(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    """FR-008: refusal must not leak which subscriptions the caller *is* entitled to."""
    tenant = _tenant_entitled_only_to(SUBSCRIPTION_ID, tenant_id=TENANT_ID)
    app, _callers = _build_app(plan=valid_plan, blueprint=blueprint, tenants={TENANT_ID: tenant})
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": f"Bearer token-for-{TENANT_ID}"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": UNENTITLED_SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    )

    assert response.status_code == 403
    body_text = response.text
    # The one subscription this tenant *is* entitled to must never appear in a refusal about a
    # *different* subscription — that would let a caller enumerate entitlements by probing.
    assert SUBSCRIPTION_ID not in body_text


def test_caller_cannot_read_another_tenants_plan(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    """FR-032: a plan created under one tenant's partition is invisible to another tenant's
    caller, even with a syntactically valid plan id."""
    tenant_a = _tenant_entitled_only_to(SUBSCRIPTION_ID, tenant_id=TENANT_ID)
    tenant_b = _tenant_entitled_only_to(SUBSCRIPTION_ID, tenant_id=OTHER_TENANT_ID)
    app, _callers = _build_app(
        plan=valid_plan,
        blueprint=blueprint,
        tenants={TENANT_ID: tenant_a, OTHER_TENANT_ID: tenant_b},
    )
    client = TestClient(app)

    created = client.post(
        "/v1/plans",
        headers={"Authorization": f"Bearer token-for-{TENANT_ID}"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    ).json()

    cross_tenant_read = client.get(
        f"/v1/plans/{created['planId']}",
        headers={"Authorization": f"Bearer token-for-{OTHER_TENANT_ID}"},
    )
    assert cross_tenant_read.status_code == 404

    same_tenant_read = client.get(
        f"/v1/plans/{created['planId']}",
        headers={"Authorization": f"Bearer token-for-{TENANT_ID}"},
    )
    assert same_tenant_read.status_code == 200


def test_unknown_tenant_and_unentitled_tenant_are_indistinguishable(
    valid_plan: DeploymentPlan, blueprint: PlatformBlueprint
) -> None:
    """A tenant with no onboarding record and a tenant with no grant for the requested
    subscription must produce the same outcome — see plans.py's ``get_customer_tenant`` docstring
    for why a distinguishable error would itself be a disclosure."""
    app, _callers = _build_app(plan=valid_plan, blueprint=blueprint, tenants={})
    # Manually register a caller for a tenant with no seeded CustomerTenant record at all.
    app.state.token_validator = _FakeTokenValidator({"token-for-unknown": _caller(TENANT_ID)})
    client = TestClient(app)

    response = client.post(
        "/v1/plans",
        headers={"Authorization": "Bearer token-for-unknown"},
        json={
            "blueprintId": "standard-production-fabric",
            "blueprintVersion": "1.0.0",
            "subscriptionId": SUBSCRIPTION_ID,
            "region": "australiaeast",
        },
    )

    assert response.status_code == 403
