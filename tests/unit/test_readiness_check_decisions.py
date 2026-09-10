"""T036, T041 — the pure decision logic inside the readiness check modules.

The real Azure SDK call in each check module is Microsoft's to get right (see each module's own
docstring). What belongs to this test file is the decision each check makes given a hypothetical
API response — exercised directly, with no network dependency.
"""

from __future__ import annotations

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.checks.naming import (
    _evaluate_existence,
    deployment_resource_group_name,
)
from groundwork_shared.validation.checks.tenant import _evaluate_subscription_state

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


# --- tenant.subscription-reachable --------------------------------------------------


def test_enabled_subscription_passes() -> None:
    status, finding = _evaluate_subscription_state(SUBSCRIPTION_ID, "Enabled")

    assert status is ValidationStatus.PASSED
    assert SUBSCRIPTION_ID in finding


def test_disabled_subscription_fails() -> None:
    status, finding = _evaluate_subscription_state(SUBSCRIPTION_ID, "Disabled")

    assert status is ValidationStatus.FAILED
    assert "Disabled" in finding


def test_warned_subscription_fails() -> None:
    """Warned is not a hard block state in Azure's own terms, but it is not Enabled either, and
    FR-014's assertion is specifically that the subscription is ready — not merely not-yet-blocked.
    """
    status, _ = _evaluate_subscription_state(SUBSCRIPTION_ID, "Warned")

    assert status is ValidationStatus.FAILED


# --- naming.resource-group-not-in-use ------------------------------------------------


def test_resource_group_name_is_deterministic() -> None:
    assert deployment_resource_group_name(SUBSCRIPTION_ID) == deployment_resource_group_name(
        SUBSCRIPTION_ID
    )


def test_resource_group_name_derives_from_subscription_id() -> None:
    name = deployment_resource_group_name(SUBSCRIPTION_ID)

    assert name.startswith("rg-groundwork-")
    assert SUBSCRIPTION_ID[:8] in name


def test_existing_resource_group_fails() -> None:
    status, finding = _evaluate_existence("rg-groundwork-33333333", exists=True)

    assert status is ValidationStatus.FAILED
    assert "already exists" in finding


def test_available_resource_group_passes() -> None:
    status, finding = _evaluate_existence("rg-groundwork-33333333", exists=False)

    assert status is ValidationStatus.PASSED
    assert "available" in finding
