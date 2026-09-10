"""T046 — the schema-boundary adapter (the deterministic-execution boundary, ADR-0001, FR-011).

Complements ``tests/contract/test_plan_boundary.py`` (which exercises ``DeploymentPlan`` directly)
by proving the specific function every piece of raw model output is required to pass through.
"""

from __future__ import annotations

import pytest

from groundwork_contracts.errors import PlanValidationError
from groundwork_contracts.plan import DeploymentPlan
from groundwork_controlplane.agents.boundary import validate_model_output


def test_valid_output_becomes_a_deployment_plan(plan_payload: dict[str, object]) -> None:
    plan = validate_model_output(plan_payload)

    assert isinstance(plan, DeploymentPlan)
    assert plan.blueprint_id == "standard-production-fabric"


def test_missing_required_field_is_rejected(plan_payload: dict[str, object]) -> None:
    del plan_payload["subscription_id"]

    with pytest.raises(PlanValidationError) as excinfo:
        validate_model_output(plan_payload)

    assert any("subscription_id" in error for error in excinfo.value.errors)


def test_unexpected_extra_field_is_rejected(plan_payload: dict[str, object]) -> None:
    """No coercion means an extra field fails the whole request, not a warning and a strip."""
    plan_payload["injectedField"] = "anything"

    with pytest.raises(PlanValidationError):
        validate_model_output(plan_payload)


def test_wrong_type_is_rejected(plan_payload: dict[str, object]) -> None:
    plan_payload["estimated_duration_minutes"] = "not a number"

    with pytest.raises(PlanValidationError):
        validate_model_output(plan_payload)


def test_non_mapping_input_is_rejected() -> None:
    """Malformed or truncated output — not even a mapping — is rejected cleanly, not left to raise
    an unhandled exception downstream of this boundary."""
    with pytest.raises(PlanValidationError):
        validate_model_output("this is not json object output at all")


def test_every_validation_failure_is_reported_not_just_the_first(
    plan_payload: dict[str, object],
) -> None:
    del plan_payload["subscription_id"]
    del plan_payload["region"]

    with pytest.raises(PlanValidationError) as excinfo:
        validate_model_output(plan_payload)

    assert len(excinfo.value.errors) >= 2


def test_forbidden_field_tenant_id_is_rejected(plan_payload: dict[str, object]) -> None:
    """FR-007 — a model can never supply its own tenant. The field does not exist on the schema at
    all, so supplying it is just another extra-field rejection, proven here at the boundary
    function actually used at runtime."""
    plan_payload["tenant_id"] = "11111111-1111-1111-1111-111111111111"

    with pytest.raises(PlanValidationError):
        validate_model_output(plan_payload)
