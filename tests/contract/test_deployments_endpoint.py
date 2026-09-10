"""T056 — ``POST /deployments`` and the status monitor.

Every successful creation reports ``status: "queued"`` — see ``api/deployments.py``'s module
docstring for why that is the honest answer today, not a temporary placeholder this file should
stop asserting once the execution engine exists. When the sequencer (T070) lands, this file is
exactly where the "actually goes to executing" behaviour should get its own new test.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.approval import (
    ActorKind,
    Approval,
    ApprovalChannel,
    ApprovedParameters,
    ApprovingIdentity,
    CostComponent,
    CostComponentKind,
    CostEstimate,
    PendingApproval,
    ThresholdPolicy,
)
from groundwork_contracts.audit import AuthorityChain, DeploymentStageRecord
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import SealedDeploymentPlan
from groundwork_contracts.tenant import ConsentState, CustomerTenant
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.deployments import router as deployments_router
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
OTHER_APPROVAL_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAN_HASH = "sha256:" + "c" * 64
OTHER_PLAN_HASH = "sha256:" + "d" * 64
NOW = datetime(2026, 8, 1, tzinfo=UTC)

_BLUEPRINT = load_blueprint(
    Path(__file__).resolve().parents[2]
    / "infra"
    / "blueprints"
    / "standard-production-fabric"
    / "blueprint.yaml"
)
_SANDBOX_BLUEPRINT = load_blueprint(
    Path(__file__).resolve().parents[2] / "infra" / "blueprints" / "dev-sandbox" / "blueprint.yaml"
)


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
        bound_value = next((p["value"] for p in (parameters or []) if p["name"] == "@value"), None)
        for (tenant, _item_id), doc in self._items.items():
            if tenant != partition_key:
                continue
            if "c.id = @value" in query and doc.get("id") != bound_value:
                continue
            if "c.plan_hash = @value" in query and doc.get("plan_hash") != bound_value:
                continue
            if "second_approval" in query:
                has_second = "second_approval" in doc
                wants_second = "NOT IS_DEFINED" not in query
                if has_second != wants_second:
                    continue
            if "authority.approval_id" in query:
                wanted = next(p["value"] for p in (parameters or []) if p["name"] == "@approval_id")
                if doc.get("authority", {}).get("approval_id") != wanted:
                    continue
            if "c.status IN" in query and doc.get("status") not in ("queued", "executing"):
                continue
            if "c.deployment_id = @deployment_id" in query:
                wanted = next(
                    p["value"] for p in (parameters or []) if p["name"] == "@deployment_id"
                )
                if doc.get("deployment_id") != wanted:
                    continue
            yield doc

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body


class _FakeTokenValidator:
    def __init__(self, caller: AuthenticatedCaller) -> None:
        self._caller = caller

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        if authorization_header != "Bearer good-token":
            raise AuthenticationError("no authorization header")
        return self._caller


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def _caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER, CallerRole.APPROVER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _tenant(*, concurrency_cap: int = 3) -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        consent_state=ConsentState.GRANTED,
        consent_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        concurrency_cap=concurrency_cap,
    )


def _cost_estimate() -> CostEstimate:
    return CostEstimate(
        components=(
            CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=400.0),
        ),
        monthly_total=400.0,
        uncertainty_lower_pct=10.0,
        uncertainty_upper_pct=20.0,
        basis="test fixture",
        computed_at=NOW,
        powerbi_viewer_licensing_disclosed=True,
    )


def _complete_approval(*, approval_id: str = APPROVAL_ID, plan_hash: str = PLAN_HASH) -> Approval:
    return Approval(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        plan_hash=plan_hash,
        approving_identity=ApprovingIdentity(
            object_id=REQUESTER_ID, display_name="Approver", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/1.json",
        approved_parameters=ApprovedParameters(
            tenant_id=TENANT_ID,
            subscription_id=SUBSCRIPTION_ID,
            region="australiaeast",
            fabric_capacity_sku="F2",
            environment="production",
            monthly_total_aud=400.0,
        ),
        cost_estimate=_cost_estimate(),
        threshold_applied=ThresholdPolicy(
            monthly_amount_aud=100000.0, approver_role="Groundwork.Approver"
        ),
        second_approval=None,
    )


def _pending_approval(
    *, approval_id: str = APPROVAL_ID, plan_hash: str = PLAN_HASH
) -> PendingApproval:
    return PendingApproval(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        plan_hash=plan_hash,
        approving_identity=ApprovingIdentity(
            object_id=REQUESTER_ID, display_name="Approver", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/1.json",
        approved_parameters=ApprovedParameters(
            tenant_id=TENANT_ID,
            subscription_id=SUBSCRIPTION_ID,
            region="australiaeast",
            fabric_capacity_sku="F2",
            environment="production",
            monthly_total_aud=400.0,
        ),
        cost_estimate=_cost_estimate(),
        threshold_applied=ThresholdPolicy(
            monthly_amount_aud=1.0, approver_role="Groundwork.Approver"
        ),
    )


def _build_app(
    *,
    tenant: CustomerTenant,
    approval: Approval | PendingApproval | None,
    sealed_plan: SealedDeploymentPlan | None = None,
) -> tuple[FastAPI, _FakeContainer, _FakeContainer]:
    app = FastAPI()
    app.include_router(deployments_router)
    register_error_handlers(app)

    tenant_container = _FakeContainer()
    tenant_doc = tenant.model_dump(mode="json") | {
        "id": tenant.tenant_id,
        "tenantId": tenant.tenant_id,
    }
    tenant_container.seed(tenant.tenant_id, tenant.tenant_id, tenant_doc)
    app.state.tenant_repository = TenantScopedRepository(
        tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
    )

    approvals_container = _FakeContainer()
    if approval is not None:
        doc = approval.model_dump(mode="json") | {
            "id": approval.approval_id,
            "tenantId": approval.tenant_id,
        }
        approvals_container.seed(approval.tenant_id, approval.approval_id, doc)
    app.state.approval_repository = TenantScopedRepository(
        approvals_container, model_cls=Approval, id_field="approval_id"
    )
    app.state.pending_approval_repository = TenantScopedRepository(
        approvals_container, model_cls=PendingApproval, id_field="approval_id"
    )

    deployment_container = _FakeContainer()
    app.state.deployment_repository = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )

    plan_container = _FakeContainer()
    if sealed_plan is not None:
        plan_container.seed(
            sealed_plan.tenant_id,
            sealed_plan.plan_hash,
            sealed_plan.model_dump(mode="json")
            | {"id": sealed_plan.plan_hash, "tenantId": sealed_plan.tenant_id},
        )
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    app.state.blueprints = {
        _BLUEPRINT.blueprint_id: _BLUEPRINT,
        _SANDBOX_BLUEPRINT.blueprint_id: _SANDBOX_BLUEPRINT,
    }

    stage_record_container = _FakeContainer()
    app.state.stage_record_repository = TenantScopedRepository(
        stage_record_container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    app.state.blueprint = _BLUEPRINT

    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.credential = _FakeCredential()

    return app, deployment_container, approvals_container


def test_create_deployment_is_always_queued(valid_plan: Any) -> None:
    sealed_plan = SealedDeploymentPlan.seal(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=NOW,
    )
    app, _container, _approvals = _build_app(
        tenant=_tenant(), approval=_complete_approval(), sealed_plan=sealed_plan
    )
    client = TestClient(app)

    response = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    assert "deploymentId" in body
    assert response.headers["Operation-Location"] == f"/v1/deployments/{body['deploymentId']}"


def test_repeatability_header_returns_the_same_deployment() -> None:
    app, container, _approvals = _build_app(tenant=_tenant(), approval=_complete_approval())
    client = TestClient(app)
    repeat_id = "eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"

    first = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token", "Repeatability-Request-ID": repeat_id},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )
    second = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token", "Repeatability-Request-ID": repeat_id},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )

    assert first.status_code == 202
    assert second.status_code == 202
    assert first.json()["deploymentId"] == second.json()["deploymentId"] == repeat_id
    assert len([k for k in container._items if k[1] == repeat_id]) == 1


def test_invalid_repeatability_header_is_400() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=_complete_approval())
    client = TestClient(app)

    response = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token", "Repeatability-Request-ID": "not-a-guid"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )

    assert response.status_code == 400


def test_unknown_approval_is_404() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=None)
    client = TestClient(app)

    response = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )

    assert response.status_code == 404


def test_incomplete_approval_is_409() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=_pending_approval())
    client = TestClient(app)

    response = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )

    assert response.status_code == 409


def test_superseded_plan_hash_is_409() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=_complete_approval())
    client = TestClient(app)

    response = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": OTHER_PLAN_HASH},
    )

    assert response.status_code == 409


def test_approval_already_used_is_409() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=_complete_approval())
    client = TestClient(app)
    request_body = {"approvalId": APPROVAL_ID, "planHash": PLAN_HASH}

    first = client.post(
        "/v1/deployments", headers={"Authorization": "Bearer good-token"}, json=request_body
    )
    assert first.status_code == 202

    second = client.post(
        "/v1/deployments", headers={"Authorization": "Bearer good-token"}, json=request_body
    )
    assert second.status_code == 409


def test_tenant_at_cap_queues_new_deployment_behind_others() -> None:
    app, _container, approvals = _build_app(
        tenant=_tenant(concurrency_cap=1), approval=_complete_approval()
    )
    client = TestClient(app)

    first = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    )
    assert first.status_code == 202
    assert first.json()["queuePosition"] is None

    # A second, distinct approval for a different plan, same tenant already at its cap of 1.
    second_approval = _complete_approval(approval_id=OTHER_APPROVAL_ID, plan_hash=OTHER_PLAN_HASH)
    doc = second_approval.model_dump(mode="json") | {
        "id": second_approval.approval_id,
        "tenantId": second_approval.tenant_id,
    }
    approvals.seed(TENANT_ID, second_approval.approval_id, doc)

    second = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": OTHER_APPROVAL_ID, "planHash": OTHER_PLAN_HASH},
    )
    assert second.status_code == 202
    # The first deployment is itself status="queued" (nothing ever transitions to "executing"
    # yet — see api/deployments.py's module docstring), so it already counts as one of the
    # tenant's active_count=1 slots against cap=1 by the time the second request is evaluated.
    # The second deployment's queue_position is therefore 2: one queued deployment (the first)
    # ahead of it, so it becomes the second entry in the wait queue.
    assert second.json()["queuePosition"] == 2


def test_get_deployment_is_scoped_to_the_callers_tenant() -> None:
    app, _container, _approvals = _build_app(tenant=_tenant(), approval=_complete_approval())
    client = TestClient(app)

    created = client.post(
        "/v1/deployments",
        headers={"Authorization": "Bearer good-token"},
        json={"approvalId": APPROVAL_ID, "planHash": PLAN_HASH},
    ).json()

    found = client.get(
        f"/v1/deployments/{created['deploymentId']}", headers={"Authorization": "Bearer good-token"}
    )
    assert found.status_code == 200
    assert found.json()["status"] == "queued"

    missing = client.get(
        "/v1/deployments/ffffffff-ffff-ffff-ffff-ffffffffffff",
        headers={"Authorization": "Bearer good-token"},
    )
    assert missing.status_code == 404


def test_get_deployment_resolves_recovery_shape_from_the_plans_blueprint(valid_plan: Any) -> None:
    sandbox_plan = SealedDeploymentPlan.seal(
        valid_plan.model_copy(update={"blueprint_id": "dev-sandbox", "blueprint_version": "0.1.0"}),
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=NOW,
    )
    app, container, _approvals = _build_app(
        tenant=_tenant(), approval=_complete_approval(), sealed_plan=sandbox_plan
    )
    client = TestClient(app)

    halted = Deployment(
        deployment_id="ffffffff-ffff-ffff-ffff-ffffffffffff",
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id="12121212-1212-1212-1212-121212121212",
        authority=AuthorityChain(plan_hash=sandbox_plan.plan_hash, approval_id=APPROVAL_ID),
        status=DeploymentStatus.HALTED,
        current_stage=None,
        started_at=NOW,
        completed_at=NOW,
    )
    container.seed(
        TENANT_ID,
        halted.deployment_id,
        halted.model_dump(mode="json") | {"id": halted.deployment_id, "tenantId": TENANT_ID},
    )

    response = client.get(
        f"/v1/deployments/{halted.deployment_id}", headers={"Authorization": "Bearer good-token"}
    )

    assert response.status_code == 200, response.text
    assert response.json()["status"] == "halted"
