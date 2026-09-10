"""T085 — ``POST /deployments/{deploymentId}/recovery``.

See ``api/recovery.py``'s own module docstring for why retry/forward-fix are both real, working
requeues here, while a validated rollback request answers ``501`` — a disclosed scope boundary,
not a silent stub.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
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
from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentStageRecord,
    StageError,
    StageStatus,
)
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.recovery import router as recovery_router
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
ROLLBACK_APPROVAL_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAN_HASH = "sha256:" + "c" * 64
OTHER_PLAN_HASH = "sha256:" + "d" * 64
NOW = datetime(2026, 8, 1, tzinfo=UTC)
ROLLBACK_RUN_ID = 4242

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
            if partition_key is not None and tenant != partition_key:
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


def _caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=REQUESTER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER, CallerRole.APPROVER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
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


def _approval(*, approval_id: str, plan_hash: str = PLAN_HASH) -> Approval:
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


def _pending_approval(*, approval_id: str, plan_hash: str = PLAN_HASH) -> PendingApproval:
    return PendingApproval(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        plan_hash=plan_hash,
        approving_identity=ApprovingIdentity(
            object_id=REQUESTER_ID, display_name="Approver", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/2.json",
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


def _halted_deployment(
    *,
    current_stage_that_failed: str = "devops_project",
    infrastructure_outputs: str | None = None,
    plan_hash: str = PLAN_HASH,
) -> Deployment:
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=plan_hash, approval_id=APPROVAL_ID),
        status=DeploymentStatus.HALTED,
        current_stage=None,
        checkpoint=None,
        infrastructure_outputs=infrastructure_outputs,
        started_at=NOW,
        completed_at=NOW,
    )


def _failed_stage_record(
    *, stage_name: str = "devops_project", is_transient: bool = False
) -> DeploymentStageRecord:
    from groundwork_contracts.audit import RecoveryPath

    return DeploymentStageRecord(
        record_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        stage_name=stage_name,
        attempt=3,
        status=StageStatus.FAILED,
        started_at=NOW,
        ended_at=NOW,
        error=StageError(code="AuthzDenied", message="forbidden", is_transient=is_transient),
        recovery_path=RecoveryPath.FORWARD_FIX,
    )


def _build_app(
    *,
    deployment: Deployment | None,
    stage_record: DeploymentStageRecord | None,
    approvals: list[Approval | PendingApproval] | None = None,
    tenant_devops_organization_url: str | None = "https://dev.azure.com/example-org",
    http_client: httpx.AsyncClient | None = None,
    sealed_plan: SealedDeploymentPlan | None = None,
) -> tuple[FastAPI, _FakeContainer]:
    app = FastAPI()
    app.include_router(recovery_router)
    register_error_handlers(app)

    deployment_container = _FakeContainer()
    if deployment is not None:
        doc = deployment.model_dump(mode="json") | {
            "id": deployment.deployment_id,
            "tenantId": deployment.tenant_id,
        }
        deployment_container.seed(deployment.tenant_id, deployment.deployment_id, doc)
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
    if stage_record is not None:
        doc = stage_record.model_dump(mode="json") | {
            "id": stage_record.record_id,
            "tenantId": stage_record.tenant_id,
        }
        stage_record_container.seed(stage_record.tenant_id, stage_record.record_id, doc)
    app.state.stage_record_repository = TenantScopedRepository(
        stage_record_container, model_cls=DeploymentStageRecord, id_field="record_id"
    )

    approvals_container = _FakeContainer()
    for approval in approvals or []:
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

    app.state.blueprint = _BLUEPRINT
    app.state.token_validator = _FakeTokenValidator(_caller())
    app.state.http_client = http_client
    app.state.credential = _FakeCredential()
    app.state.now_fn = lambda: NOW

    class _ReadinessSettings:
        def __init__(self, devops_organization_url: str | None) -> None:
            self.devops_organization_url = devops_organization_url

    class _Settings:
        def __init__(self, devops_organization_url: str | None) -> None:
            self.readiness = _ReadinessSettings(devops_organization_url)

    app.state.settings = _Settings(tenant_devops_organization_url)

    class _Tenant:
        def __init__(self, devops_organization_url: str | None) -> None:
            self.devops_organization_url = devops_organization_url

    class _TenantRepository:
        def __init__(self, devops_organization_url: str | None) -> None:
            self._tenant = _Tenant(devops_organization_url)

        async def read(self, _partition_key: str, _item_id: str) -> Any:
            return self._tenant

    app.state.tenant_repository = _TenantRepository(tenant_devops_organization_url)

    return app, deployment_container


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def test_unknown_deployment_is_404() -> None:
    app, _ = _build_app(deployment=None, stage_record=None)
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "retry"},
    )

    assert response.status_code == 404


def test_deployment_not_halted_is_409() -> None:
    deployment = _halted_deployment().model_copy(
        update={"status": DeploymentStatus.SUCCEEDED, "checkpoint": None}
    )
    app, _ = _build_app(deployment=deployment, stage_record=None)
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "retry"},
    )

    assert response.status_code == 409


def test_retry_requeues_the_deployment() -> None:
    app, container = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="devops_project"),
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "retry"},
    )

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"
    saved = container._items[(TENANT_ID, DEPLOYMENT_ID)]
    assert saved["status"] == "queued"
    assert saved["current_stage"] is None
    assert saved["started_at"] is None
    assert saved["completed_at"] is None


def test_forward_fix_requeues_the_deployment() -> None:
    app, _container = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="devops_project"),
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "forward_fix"},
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"


def test_action_not_offered_for_the_failing_stage_is_422() -> None:
    # devops_project's own recoveryPath never mentions rollback.
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="devops_project"),
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 422


def test_recovery_options_come_from_the_deployments_blueprint(valid_plan: DeploymentPlan) -> None:
    sandbox_plan = SealedDeploymentPlan.seal(
        DeploymentPlan.model_validate(
            valid_plan.model_copy(
                update={"blueprint_id": "dev-sandbox", "blueprint_version": "0.1.0"}
            ).model_dump(mode="json")
        ),
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=NOW,
    )
    app, _ = _build_app(
        deployment=_halted_deployment(plan_hash=sandbox_plan.plan_hash),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        sealed_plan=sandbox_plan,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 422
    assert "rollback" not in response.json()["detail"].split("available:", maxsplit=1)[1]


def test_rollback_without_approval_id_is_403() -> None:
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback"},
    )

    assert response.status_code == 403


def test_rollback_approval_not_found_is_404() -> None:
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 404


def test_rollback_approval_incomplete_is_409() -> None:
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        approvals=[_pending_approval(approval_id=ROLLBACK_APPROVAL_ID)],
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 409


def test_rollback_approval_for_a_different_plan_is_409() -> None:
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        approvals=[_approval(approval_id=ROLLBACK_APPROVAL_ID, plan_hash=OTHER_PLAN_HASH)],
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 409


def test_rollback_reusing_the_original_approval_id_is_403() -> None:
    app, _ = _build_app(
        deployment=_halted_deployment(),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        approvals=[_approval(approval_id=APPROVAL_ID)],
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": APPROVAL_ID},
    )

    assert response.status_code == 403


def test_valid_rollback_request_returns_202_and_marks_rolled_back() -> None:
    calls = {"triggered": 0, "polled": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 43,
                            "name": "groundwork-33333333-rollback-pipeline",
                        }
                    ]
                },
            )
        if request.method == "POST" and "/pipelines/43/runs" in path:
            calls["triggered"] += 1
            payload = json.loads(request.content.decode("utf-8"))
            assert payload["templateParameters"]["rollbackToOutputs"] == (
                '{"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-'
                "333333333333/resourceGroups/rg-groundwork-33333333/providers/Microsoft.Managed"
                'Identity/userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":'
                '{"value":"rg-groundwork-33333333"}}'
            )
            return httpx.Response(200, json={"id": ROLLBACK_RUN_ID, "state": "inProgress"})
        if request.method == "GET" and f"/runs/{ROLLBACK_RUN_ID}" in path:
            calls["polled"] += 1
            return httpx.Response(
                200, json={"id": ROLLBACK_RUN_ID, "state": "completed", "result": "succeeded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app, container = _build_app(
        deployment=_halted_deployment(
            current_stage_that_failed="infrastructure",
            infrastructure_outputs='{"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-groundwork-33333333/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":{"value":"rg-groundwork-33333333"}}',
        ),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        approvals=[_approval(approval_id=ROLLBACK_APPROVAL_ID)],
        http_client=http_client,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "rolled_back"
    saved = container._items[(TENANT_ID, DEPLOYMENT_ID)]
    assert saved["status"] == "rolled_back"
    assert calls == {"triggered": 1, "polled": 1}
    import asyncio

    asyncio.run(http_client.aclose())
