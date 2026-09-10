"""T075b — execution-time cost escalation security test (FR-019, SC-018).

A deployment halted by T075a's cost re-check because the recomputed figure crosses the FR-020a
second-approver threshold must stay refused until a *distinct* second approver acts on a *fresh*
approval — never resolvable by reusing the original single approval, and never resolvable by a new
approval that is itself only a single self-approval.
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
    SecondApproval,
    ThresholdPolicy,
)
from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentStageRecord,
    RecoveryPath,
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

pytestmark = pytest.mark.security

TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
FIRST_APPROVER_ID = "44444444-4444-4444-4444-444444444444"
SECOND_APPROVER_ID = "55555555-5555-5555-5555-555555555555"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
ORIGINAL_APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
FRESH_APPROVAL_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
PLAN_HASH = "sha256:" + "c" * 64
NOW = datetime(2026, 8, 2, tzinfo=UTC)

_BLUEPRINT = load_blueprint(
    Path(__file__).resolve().parents[2]
    / "infra"
    / "blueprints"
    / "standard-production-fabric"
    / "blueprint.yaml"
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


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def _caller() -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=FIRST_APPROVER_ID,
        tenant_id=TENANT_ID,
        display_name="Test Requester",
        roles=frozenset({CallerRole.REQUESTER, CallerRole.APPROVER}),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _cost_estimate(total: float = 150_000.0) -> CostEstimate:
    return CostEstimate(
        components=(
            CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=total),
        ),
        monthly_total=total,
        uncertainty_lower_pct=10.0,
        uncertainty_upper_pct=20.0,
        basis="test fixture",
        computed_at=NOW,
        powerbi_viewer_licensing_disclosed=True,
    )


def _approved_parameters(total: float) -> ApprovedParameters:
    return ApprovedParameters(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        region="australiaeast",
        fabric_capacity_sku="F64",
        environment="production",
        monthly_total_aud=total,
    )


def _fresh_approval(
    *, approval_id: str, second_approval: SecondApproval | None, total: float = 150_000.0
) -> Approval:
    return Approval(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        plan_hash=PLAN_HASH,
        approving_identity=ApprovingIdentity(
            object_id=FIRST_APPROVER_ID, display_name="Requester", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/fresh.json",
        approved_parameters=_approved_parameters(total),
        cost_estimate=_cost_estimate(total),
        threshold_applied=ThresholdPolicy(
            monthly_amount_aud=100_000.0, approver_role="Groundwork.Approver"
        ),
        second_approval=second_approval,
    )


def _fresh_pending_approval(*, approval_id: str, total: float = 150_000.0) -> PendingApproval:
    return PendingApproval(
        approval_id=approval_id,
        tenant_id=TENANT_ID,
        plan_hash=PLAN_HASH,
        approving_identity=ApprovingIdentity(
            object_id=FIRST_APPROVER_ID, display_name="Requester", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/pending.json",
        approved_parameters=_approved_parameters(total),
        cost_estimate=_cost_estimate(total),
        threshold_applied=ThresholdPolicy(
            monthly_amount_aud=100_000.0, approver_role="Groundwork.Approver"
        ),
    )


def _second_approval(identity: str = SECOND_APPROVER_ID) -> SecondApproval:
    return SecondApproval(
        approving_identity=ApprovingIdentity(
            object_id=identity, display_name="Finance Approver", actor_kind=ActorKind.HUMAN
        ),
        approved_at=NOW,
        channel=ApprovalChannel.TEAMS,
        artefact_uri="https://example.invalid/approvals/second.json",
    )


def _halted_deployment() -> Deployment:
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=ORIGINAL_APPROVAL_ID),
        status=DeploymentStatus.HALTED,
        current_stage=None,
        checkpoint=None,
        started_at=NOW,
        completed_at=NOW,
    )


def _cost_reapproval_stage_record(*, escalation_required: bool) -> DeploymentStageRecord:
    return DeploymentStageRecord(
        record_id="cccccccc-cccc-cccc-cccc-cccccccccccc",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        stage_name="cost_reapproval",
        attempt=1,
        status=StageStatus.FAILED,
        started_at=NOW,
        ended_at=NOW,
        error=StageError(
            code=(
                "CostReapprovalEscalationRequired"
                if escalation_required
                else "CostReapprovalRequired"
            ),
            message="recomputed monthly total is outside the approved band",
            is_transient=False,
        ),
        recovery_path=RecoveryPath.FORWARD_FIX,
    )


def _build_app(
    *,
    stage_record: DeploymentStageRecord,
    approvals: list[Approval | PendingApproval] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.include_router(recovery_router)
    register_error_handlers(app)

    deployment_container = _FakeContainer()
    deployment = _halted_deployment()
    doc = deployment.model_dump(mode="json") | {
        "id": deployment.deployment_id,
        "tenantId": deployment.tenant_id,
    }
    deployment_container.seed(deployment.tenant_id, deployment.deployment_id, doc)
    app.state.deployment_repository = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )

    plan_container = _FakeContainer()
    sealed_plan = SealedDeploymentPlan.seal(
        DeploymentPlan.model_validate(
            {
                "schemaVersion": "1.0.0",
                "blueprintId": "standard-production-fabric",
                "blueprintVersion": "1.0.0",
                "subscriptionId": SUBSCRIPTION_ID,
                "region": "australiaeast",
                "environment": "production",
                "fabricCapacitySku": "F64",
                "resourceSet": [
                    {
                        "resourceType": "Microsoft.Fabric/capacities",
                        "logicalName": "fab-groundwork-001",
                    }
                ],
                "estimatedDurationMinutes": 45,
                "costEstimate": {
                    "currency": "AUD",
                    "monthlyTotal": 150000.0,
                    "uncertaintyLowerPct": 10.0,
                    "uncertaintyUpperPct": 20.0,
                    "basis": "test fixture",
                    "computedAt": NOW.isoformat(),
                },
                "riskAssessment": {"severity": "low", "findings": []},
            }
        ),
        tenant_id=TENANT_ID,
        requesting_identity_object_id=FIRST_APPROVER_ID,
        requesting_channel="api",
        now=NOW,
    )
    plan_container.seed(
        TENANT_ID,
        sealed_plan.plan_hash,
        sealed_plan.model_dump(mode="json") | {"id": sealed_plan.plan_hash, "tenantId": TENANT_ID},
    )
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )
    app.state.blueprints = {_BLUEPRINT.blueprint_id: _BLUEPRINT}

    stage_record_container = _FakeContainer()
    stage_doc = stage_record.model_dump(mode="json") | {
        "id": stage_record.record_id,
        "tenantId": stage_record.tenant_id,
    }
    stage_record_container.seed(stage_record.tenant_id, stage_record.record_id, stage_doc)
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
    app.state.credential = _FakeCredential()

    return app


def _post_retry(app: FastAPI, approval_id: str | None) -> Any:
    client = TestClient(app)
    body: dict[str, object] = {"action": "retry"}
    if approval_id is not None:
        body["approvalId"] = approval_id
    return client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json=body,
    )


def test_escalated_cost_reapproval_without_approval_id_is_403() -> None:
    app = _build_app(stage_record=_cost_reapproval_stage_record(escalation_required=True))

    response = _post_retry(app, approval_id=None)

    assert response.status_code == 403


def test_escalated_cost_reapproval_reusing_original_approval_id_is_403() -> None:
    """SC-018/FR-019: the original approval — the one whose stale cost figure caused this halt in
    the first place — must never be reusable to resolve it, even though it is a real, complete
    approval on file."""
    original = _fresh_approval(
        approval_id=ORIGINAL_APPROVAL_ID, second_approval=_second_approval(), total=412.50
    )
    app = _build_app(
        stage_record=_cost_reapproval_stage_record(escalation_required=True),
        approvals=[original],
    )

    response = _post_retry(app, approval_id=ORIGINAL_APPROVAL_ID)

    assert response.status_code == 403


def test_escalated_cost_reapproval_with_single_self_approval_is_refused() -> None:
    """The core SC-018 guarantee this test exists for: a fresh approval that is real, complete,
    correctly scoped to this plan, and distinct from the original — but is only a *single*
    self-approval — must still be refused once escalation was recorded at halt time.

    ``Approval`` itself already refuses to represent a single self-approval *whose own cost figure*
    crosses the threshold (its ``_second_approver_present_and_distinct`` validator) — so the
    realistic gap this endpoint-level check exists to close is a fresh approval obtained *below*
    threshold (e.g. the customer approved at $95k without realising the live figure had moved)
    while the halted deployment's own recomputed cost crossed it independently. Silently accepting
    that approval would let one identity satisfy what is, in substance, an escalated,
    two-person-required decision."""
    fresh_single = _fresh_approval(
        approval_id=FRESH_APPROVAL_ID, second_approval=None, total=95_000.0
    )
    app = _build_app(
        stage_record=_cost_reapproval_stage_record(escalation_required=True),
        approvals=[fresh_single],
    )

    response = _post_retry(app, approval_id=FRESH_APPROVAL_ID)

    assert response.status_code == 409
    assert "second-approver" in response.json()["detail"].lower()


def test_escalated_cost_reapproval_pending_second_approver_is_refused() -> None:
    """A fresh approval that is still only *pending* its second approver (no second_approval yet
    recorded at all) must be refused exactly like a genuinely single-approval one — SC-018 admits
    no partial-credit state."""
    pending = _fresh_pending_approval(approval_id=FRESH_APPROVAL_ID)
    app = _build_app(
        stage_record=_cost_reapproval_stage_record(escalation_required=True),
        approvals=[pending],
    )

    response = _post_retry(app, approval_id=FRESH_APPROVAL_ID)

    assert response.status_code == 409


def test_escalated_cost_reapproval_with_distinct_second_approver_succeeds() -> None:
    """The one path that must actually work: a fresh, complete, correctly-scoped approval with a
    genuinely distinct second approver resolves the halt and resumes the deployment."""
    fresh_escalated = _fresh_approval(
        approval_id=FRESH_APPROVAL_ID, second_approval=_second_approval(SECOND_APPROVER_ID)
    )
    app = _build_app(
        stage_record=_cost_reapproval_stage_record(escalation_required=True),
        approvals=[fresh_escalated],
    )

    response = _post_retry(app, approval_id=FRESH_APPROVAL_ID)

    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "queued"


def test_non_escalated_cost_reapproval_accepts_a_single_fresh_approval() -> None:
    """Below the threshold, a single fresh, distinct, correctly-scoped approval is sufficient —
    the escalation gate is specifically about crossing the FR-020a threshold, not a blanket
    requirement on every cost re-approval."""
    fresh_single = _fresh_approval(approval_id=FRESH_APPROVAL_ID, second_approval=None, total=450.0)
    app = _build_app(
        stage_record=_cost_reapproval_stage_record(escalation_required=False),
        approvals=[fresh_single],
    )

    response = _post_retry(app, approval_id=FRESH_APPROVAL_ID)

    assert response.status_code == 202
