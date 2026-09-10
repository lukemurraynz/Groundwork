"""Shared fixtures.

These build *valid* objects. Each test then breaks exactly one thing, so a failure identifies the
rule that fired rather than leaving you to work out which of six invalid fields caused it.

No fixture contains a real secret, credential, or customer identifier. GUIDs here are literal test
values, not redactions of anything real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from groundwork_contracts import (
    ApprovedRegion,
    CostComponent,
    CostComponentKind,
    CostEstimate,
    CostEstimateRef,
    DeploymentPlan,
    Environment,
    FabricCapacitySku,
    PlanResource,
    RiskAssessment,
    RiskSeverity,
    ThresholdPolicy,
)

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
APPROVER_ID = "55555555-5555-5555-5555-555555555555"
SECOND_APPROVER_ID = "66666666-6666-6666-6666-666666666666"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
PLAN_ID = "99999999-9999-9999-9999-999999999999"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"


@pytest.fixture
def now() -> datetime:
    """A fixed instant.

    Fixed rather than ``datetime.now()`` so retention and validity-window assertions are
    deterministic. A time-dependent test that passes today and fails at midnight is worse than
    no test.
    """
    return datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def cost_estimate_ref(now: datetime) -> CostEstimateRef:
    return CostEstimateRef(
        monthly_total=412.50,
        uncertainty_lower_pct=10.0,
        uncertainty_upper_pct=20.0,
        basis="Azure Retail Prices API snapshot 2026-07-30",
        computed_at=now,
    )


@pytest.fixture
def cost_estimate(now: datetime) -> CostEstimate:
    return CostEstimate(
        components=(
            CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=350.00),
            CostComponent(component=CostComponentKind.STORAGE, monthly_amount=25.50),
            CostComponent(component=CostComponentKind.NETWORKING, monthly_amount=22.00),
            CostComponent(component=CostComponentKind.MONITORING, monthly_amount=15.00),
        ),
        monthly_total=412.50,
        uncertainty_lower_pct=10.0,
        uncertainty_upper_pct=20.0,
        basis="Azure Retail Prices API snapshot 2026-07-30",
        computed_at=now,
        powerbi_viewer_licensing_disclosed=True,
    )


@pytest.fixture
def valid_plan(cost_estimate_ref: CostEstimateRef) -> DeploymentPlan:
    """A minimal valid plan — what a well-behaved agent produces."""
    return DeploymentPlan(
        blueprint_id="standard-production-fabric",
        blueprint_version="1.0.0",
        subscription_id=SUBSCRIPTION_ID,
        region=ApprovedRegion.AUSTRALIA_EAST,
        environment=Environment.PRODUCTION,
        fabric_capacity_sku=FabricCapacitySku.F2,
        resource_set=(
            PlanResource(
                resource_type="Microsoft.Resources/resourceGroups",
                logical_name="rg-groundwork-data",
            ),
            PlanResource(
                resource_type="Microsoft.Fabric/capacities",
                logical_name="fab-groundwork-001",
                depends_on=("rg-groundwork-data",),
                properties={"sku": "F2", "adminUser": "platform-admin"},
            ),
        ),
        estimated_duration_minutes=45,
        cost_estimate=cost_estimate_ref,
        risk_assessment=RiskAssessment(severity=RiskSeverity.LOW),
    )


@pytest.fixture
def plan_payload(valid_plan: DeploymentPlan) -> dict[str, object]:
    """The valid plan as a wire-shaped dict, for tests that mutate one field."""
    return valid_plan.model_dump(mode="json")


@pytest.fixture
def threshold_policy() -> ThresholdPolicy:
    return ThresholdPolicy(
        monthly_amount_aud=1000.00,
        approver_role="Groundwork.Approver",
        applies_to_environments=("production",),
    )


@pytest.fixture
def high_threshold_policy() -> ThresholdPolicy:
    """A threshold the standard fixture cost exceeds, to exercise the second-approver path."""
    return ThresholdPolicy(
        monthly_amount_aud=100.00,
        approver_role="Groundwork.Approver",
        applies_to_environments=("production",),
    )


@pytest.fixture
def retention_ok(now: datetime) -> datetime:
    return now + timedelta(days=365)
