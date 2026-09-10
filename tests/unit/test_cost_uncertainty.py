"""T033 — FR-018 requires a cost estimate to carry an honest, non-zero uncertainty band.

A single precise figure presented as certain is a defect regardless of which caller constructs the
estimate — the rule lives on ``CostEstimateRef`` itself, so this test targets that type directly
rather than only exercising it indirectly through ``DeploymentPlan`` (as
``test_plan_boundary.py::test_zero_uncertainty_band_is_rejected`` already does at the plan level).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from groundwork_contracts.plan import CostEstimateRef

pytestmark = pytest.mark.contract

NOW = datetime(2026, 7, 31, tzinfo=UTC)


def _estimate(*, lower: float, upper: float) -> CostEstimateRef:
    return CostEstimateRef(
        monthly_total=500.0,
        uncertainty_lower_pct=lower,
        uncertainty_upper_pct=upper,
        basis="test fixture",
        computed_at=NOW,
    )


def test_zero_zero_band_is_rejected() -> None:
    with pytest.raises(ValidationError, match="uncertainty"):
        _estimate(lower=0.0, upper=0.0)


def test_one_sided_zero_band_is_accepted() -> None:
    """FR-018 requires the band be non-zero *overall*, not that both sides be non-zero — a cost
    that can only go up (never down) from the estimate is still an honest, meaningful band."""
    estimate = _estimate(lower=0.0, upper=15.0)
    assert estimate.uncertainty_lower_pct == 0.0
    assert estimate.uncertainty_upper_pct == 15.0


def test_band_bounds_are_computed_correctly() -> None:
    estimate = _estimate(lower=10.0, upper=25.0)
    lower, upper = estimate.band_absolute()
    assert lower == pytest.approx(450.0)
    assert upper == pytest.approx(625.0)


def test_contains_checks_the_absolute_band() -> None:
    estimate = _estimate(lower=10.0, upper=25.0)
    assert estimate.contains(500.0) is True
    assert estimate.contains(449.0) is False
    assert estimate.contains(626.0) is False


def test_negative_uncertainty_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _estimate(lower=-5.0, upper=10.0)


def test_uncertainty_over_100_percent_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _estimate(lower=10.0, upper=150.0)
