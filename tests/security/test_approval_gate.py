"""Approval gating — the gated-approval rule.

Covers the rules that stop a deployment being authorised by the wrong party or nobody at all:

- an agent may never approve (FR-021)
- voice alone may now authorise an irreversible action (ADR-0011, 2026-08-02 — the
  prior FR-023 prohibition is removed; the formerly-rejecting tests now assert voice is accepted)
- above the threshold, a *distinct* second approver is required (FR-020a, SC-018)
- the distinct-identity rule survives the voice-amendment unchanged: two confirmations from the
  same identity, by any channel including voice, still fail
- approval binds to an exact plan hash and parameter set (FR-020, FR-022)
- below F64 the licensing caveat must have been disclosed (FR-013d)
"""

from __future__ import annotations

from datetime import datetime

import pytest
from pydantic import ValidationError

from groundwork_contracts import (
    ActorKind,
    Approval,
    ApprovalChannel,
    ApprovedParameters,
    ApprovingIdentity,
    CostEstimate,
    SecondApproval,
    ThresholdPolicy,
)
from tests.conftest import (
    APPROVAL_ID,
    APPROVER_ID,
    SECOND_APPROVER_ID,
    SUBSCRIPTION_ID,
    TENANT_ID,
)

pytestmark = pytest.mark.security

PLAN_HASH = "sha256:" + "a" * 64


def _parameters(total: float = 412.50, sku: str = "F2") -> ApprovedParameters:
    return ApprovedParameters(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        region="australiaeast",
        fabric_capacity_sku=sku,
        environment="production",
        monthly_total_aud=total,
    )


def _human(object_id: str = APPROVER_ID, name: str = "Platform Admin") -> ApprovingIdentity:
    return ApprovingIdentity(object_id=object_id, display_name=name, actor_kind=ActorKind.HUMAN)


def _approval(
    now: datetime,
    cost_estimate: CostEstimate,
    threshold: ThresholdPolicy,
    **overrides: object,
) -> Approval:
    kwargs: dict[str, object] = {
        "approval_id": APPROVAL_ID,
        "tenant_id": TENANT_ID,
        "plan_hash": PLAN_HASH,
        "approving_identity": _human(),
        "approved_at": now,
        "channel": ApprovalChannel.TEAMS,
        "artefact_uri": "https://example.invalid/approvals/1",
        "approved_parameters": _parameters(),
        "cost_estimate": cost_estimate,
        "threshold_applied": threshold,
    }
    kwargs.update(overrides)
    return Approval(**kwargs)  # type: ignore[arg-type]


def test_valid_approval_is_accepted(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    approval = _approval(now, cost_estimate, threshold_policy)
    assert approval.is_self_approval is True
    assert approval.authorises(PLAN_HASH, _parameters()) is True


def test_agent_may_not_approve() -> None:
    """FR-021 and the gated-approval rule — an agent never self-approves.

    Rejected at identity construction, so no approval object can carry an agent approver at all.
    """
    with pytest.raises(ValidationError, match="agent may not approve"):
        ApprovingIdentity(
            object_id=APPROVER_ID,
            display_name="Planning Agent",
            actor_kind=ActorKind.AGENT,
        )


def test_voice_can_approve(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    """ADR-0011 (2026-08-02) — voice may now authorise an irreversible action on its own.
    The prior FR-023 prohibition is removed by explicit product-owner decision."""
    approval = _approval(now, cost_estimate, threshold_policy, channel=ApprovalChannel.VOICE)
    assert approval.channel is ApprovalChannel.VOICE
    assert approval.authorises(PLAN_HASH, _parameters()) is True


def test_above_threshold_requires_a_second_approver(
    now: datetime, cost_estimate: CostEstimate, high_threshold_policy: ThresholdPolicy
) -> None:
    """FR-020a — the deployment is refused until a second approval is recorded."""
    with pytest.raises(ValidationError, match="requires a second approver"):
        _approval(now, cost_estimate, high_threshold_policy)


def test_second_approver_must_be_a_different_identity(
    now: datetime, cost_estimate: CostEstimate, high_threshold_policy: ThresholdPolicy
) -> None:
    """SC-018 — zero cases of one identity satisfying both roles.

    The realistic abuse is not impersonation; it is the same admin approving twice through two
    channels because it is 5pm and they want it done.
    """
    same_person = SecondApproval(
        approving_identity=_human(),
        approved_at=now,
        channel=ApprovalChannel.EMAIL,
        artefact_uri="https://example.invalid/approvals/2",
    )
    with pytest.raises(ValidationError, match="different identity"):
        _approval(
            now,
            cost_estimate,
            high_threshold_policy,
            second_approval=same_person,
        )


def test_distinct_identity_rule_survives_voice_amendment(
    now: datetime, cost_estimate: CostEstimate, high_threshold_policy: ThresholdPolicy
) -> None:
    """ADR-0011 relaxed the durable-artefact requirement for voice, but explicitly
    kept SC-018: two confirmations from the same identity, by any channel including voice, still
    fail. This is the one invariant the amendment does NOT touch — if it breaks here, the
    distinct-identity check was accidentally weakened while editing nearby code."""
    same_person_by_voice = SecondApproval(
        approving_identity=_human(),  # same as first approver
        approved_at=now,
        channel=ApprovalChannel.VOICE,
        artefact_uri="https://example.invalid/approvals/2",
    )
    with pytest.raises(ValidationError, match="different identity"):
        _approval(
            now,
            cost_estimate,
            high_threshold_policy,
            second_approval=same_person_by_voice,
        )


def test_second_approval_by_voice_is_accepted(
    now: datetime, cost_estimate: CostEstimate, high_threshold_policy: ThresholdPolicy
) -> None:
    """ADR-0011 — a second approver may use voice, as long as they are a *different*
    identity from the first approver (SC-018 enforced by Approval's own validator)."""
    second = SecondApproval(
        approving_identity=_human(SECOND_APPROVER_ID, "Finance Approver"),
        approved_at=now,
        channel=ApprovalChannel.VOICE,
        artefact_uri="https://example.invalid/approvals/2",
    )
    approval = _approval(now, cost_estimate, high_threshold_policy, second_approval=second)
    assert approval.second_approval is not None
    assert approval.second_approval.channel is ApprovalChannel.VOICE
    assert approval.is_self_approval is False


def test_approval_does_not_authorise_a_different_plan_hash(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    """FR-022 — any change after approval voids it."""
    approval = _approval(now, cost_estimate, threshold_policy)
    assert approval.authorises("sha256:" + "b" * 64, _parameters()) is False


def test_approval_does_not_authorise_changed_parameters(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    """A region or SKU change after approval requires re-approval, not near-enough matching."""
    approval = _approval(now, cost_estimate, threshold_policy)
    assert approval.authorises(PLAN_HASH, _parameters(sku="F64")) is False


def test_below_f64_without_licensing_disclosure_is_rejected(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    """FR-013d — the customer must have been shown the Pro/PPU viewer requirement."""
    undisclosed = cost_estimate.model_copy(update={"powerbi_viewer_licensing_disclosed": False})
    with pytest.raises(ValidationError, match="below F64"):
        _approval(now, undisclosed, threshold_policy)


def test_at_f64_licensing_disclosure_is_not_required(
    now: datetime, cost_estimate: CostEstimate, threshold_policy: ThresholdPolicy
) -> None:
    """At F64 the caveat does not apply, so its absence must not block approval."""
    undisclosed = cost_estimate.model_copy(update={"powerbi_viewer_licensing_disclosed": False})
    approval = _approval(
        now,
        undisclosed,
        threshold_policy,
        approved_parameters=_parameters(sku="F64"),
    )
    assert approval.approved_parameters.fabric_capacity_sku == "F64"


def test_cost_total_must_reconcile_with_components(now: datetime) -> None:
    """An approver must be able to reconcile the figure they are agreeing to."""
    from groundwork_contracts import CostComponent, CostComponentKind

    with pytest.raises(ValidationError, match="does not match the sum"):
        CostEstimate(
            components=(
                CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=100.0),
            ),
            monthly_total=999.0,
            uncertainty_lower_pct=10.0,
            uncertainty_upper_pct=10.0,
            basis="test",
            computed_at=now,
        )


def test_reapproval_is_driven_by_the_uncertainty_band(
    cost_estimate: CostEstimate,
) -> None:
    """FR-019 — a re-checked figure inside the band was already accepted.

    A tolerance narrower than the band would fire on pricing noise and train approvers to click
    through, which is the failure this design avoids.
    """
    lower, upper = cost_estimate.band_absolute()
    assert cost_estimate.contains(cost_estimate.monthly_total) is True
    assert cost_estimate.contains(upper - 0.01) is True
    assert cost_estimate.contains(lower + 0.01) is True
    assert cost_estimate.contains(upper + 1.00) is False
    assert cost_estimate.contains(lower - 1.00) is False
