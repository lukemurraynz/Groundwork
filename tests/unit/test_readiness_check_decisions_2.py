"""T037-T042 (remainder) — pure decision logic for identity, network, quota, policy, and devops
readiness checks.

Same reasoning as ``test_readiness_check_decisions.py``: the Azure SDK / REST call in each check
module is real and untested here; what's tested is the decision given a hypothetical response.
"""

from __future__ import annotations

import pytest

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.checks.devops import (
    _evaluate_response,
    organization_reachable,
)
from groundwork_shared.validation.checks.identity import (
    _evaluate_deny_assignments,
    _evaluate_not_owner,
)
from groundwork_shared.validation.checks.network import _evaluate_overlap, _overlaps
from groundwork_shared.validation.checks.policy import (
    _definition_name_and_scope,
    _evaluate_deny_effects,
    _is_enforced,
    _is_single_policy_definition,
)
from groundwork_shared.validation.checks.quota import _evaluate_registrations

PRINCIPAL_ID = "44444444-4444-4444-4444-444444444444"


# --- identity.deployment-identity-not-owner / security.no-blanket-deny-assignment --


def test_no_owner_assignment_passes() -> None:
    status, _ = _evaluate_not_owner(has_owner=False, principal_id=PRINCIPAL_ID)
    assert status is ValidationStatus.PASSED


def test_owner_assignment_fails() -> None:
    status, finding = _evaluate_not_owner(has_owner=True, principal_id=PRINCIPAL_ID)
    assert status is ValidationStatus.FAILED
    assert PRINCIPAL_ID in finding


def test_no_deny_assignments_passes() -> None:
    status, _ = _evaluate_deny_assignments([])
    assert status is ValidationStatus.PASSED


def test_any_deny_assignment_fails() -> None:
    status, finding = _evaluate_deny_assignments(["Block all writes"])
    assert status is ValidationStatus.FAILED
    assert "Block all writes" in finding


# --- network.vnet-address-space-available -------------------------------------------


def test_non_overlapping_ranges_do_not_overlap() -> None:
    assert _overlaps("10.0.0.0/16", "10.1.0.0/16") is False


def test_identical_ranges_overlap() -> None:
    assert _overlaps("10.0.0.0/16", "10.0.0.0/16") is True


def test_contained_range_overlaps() -> None:
    assert _overlaps("10.0.0.0/16", "10.0.1.0/24") is True


def test_no_existing_vnets_passes() -> None:
    status, _ = _evaluate_overlap("10.0.0.0/16", [])
    assert status is ValidationStatus.PASSED


def test_overlap_with_existing_vnet_fails() -> None:
    status, finding = _evaluate_overlap("10.0.0.0/16", ["10.0.0.0/8"])
    assert status is ValidationStatus.FAILED
    assert "10.0.0.0/8" in finding


def test_disjoint_existing_vnets_pass() -> None:
    status, _ = _evaluate_overlap("10.0.0.0/16", ["192.168.0.0/16", "172.16.0.0/12"])
    assert status is ValidationStatus.PASSED


# --- quota.resource-provider-registered ----------------------------------------------


def test_all_registered_passes() -> None:
    status, finding = _evaluate_registrations(
        {"Microsoft.Network": "Registered", "Microsoft.KeyVault": "Registered"}
    )
    assert status is ValidationStatus.PASSED
    assert "2" in finding


def test_one_unregistered_provider_fails() -> None:
    status, finding = _evaluate_registrations(
        {"Microsoft.Network": "Registered", "Microsoft.Fabric": "NotRegistered"}
    )
    assert status is ValidationStatus.FAILED
    assert "Microsoft.Fabric" in finding
    assert "Microsoft.Network" not in finding


# --- policy.no-denying-resource-type-policy ------------------------------------------


def test_default_enforcement_is_enforced() -> None:
    assert _is_enforced("Default") is True


def test_none_enforcement_is_enforced() -> None:
    """Azure's own docs: unset enforcementMode defaults to Default (enforced)."""
    assert _is_enforced(None) is True


def test_do_not_enforce_is_not_enforced() -> None:
    assert _is_enforced("DoNotEnforce") is False


def test_single_policy_definition_id_is_recognised() -> None:
    definition_id = "/providers/Microsoft.Authorization/policyDefinitions/abc123"
    assert _is_single_policy_definition(definition_id) is True


def test_policy_set_definition_id_is_excluded() -> None:
    definition_id = "/providers/Microsoft.Authorization/policySetDefinitions/abc123"
    assert _is_single_policy_definition(definition_id) is False


def test_built_in_definition_has_no_subscription_prefix() -> None:
    name, is_built_in = _definition_name_and_scope(
        "/providers/Microsoft.Authorization/policyDefinitions/abc123"
    )
    assert name == "abc123"
    assert is_built_in is True


def test_custom_definition_has_subscription_prefix() -> None:
    definition_id = (
        "/subscriptions/33333333-3333-3333-3333-333333333333/providers/"
        "Microsoft.Authorization/policyDefinitions/my-custom-policy"
    )
    name, is_built_in = _definition_name_and_scope(definition_id)
    assert name == "my-custom-policy"
    assert is_built_in is False


def test_no_deny_effects_passes() -> None:
    status, _ = _evaluate_deny_effects([])
    assert status is ValidationStatus.PASSED


def test_deny_effect_assignment_fails() -> None:
    status, finding = _evaluate_deny_effects(["Deny public IPs"])
    assert status is ValidationStatus.FAILED
    assert "Deny public IPs" in finding


# --- devops.organization-reachable ---------------------------------------------------


def test_200_response_passes() -> None:
    status, finding = _evaluate_response("https://dev.azure.com/example", 200)
    assert status is ValidationStatus.PASSED
    assert "example" in finding


def test_401_response_fails() -> None:
    status, finding = _evaluate_response("https://dev.azure.com/example", 401)
    assert status is ValidationStatus.FAILED
    assert "organization user" in finding


def test_403_response_fails() -> None:
    status, _ = _evaluate_response("https://dev.azure.com/example", 403)
    assert status is ValidationStatus.FAILED


def test_404_response_fails() -> None:
    status, finding = _evaluate_response("https://dev.azure.com/example", 404)
    assert status is ValidationStatus.FAILED
    assert "does not exist" in finding


def test_unexpected_status_fails() -> None:
    status, finding = _evaluate_response("https://dev.azure.com/example", 503)
    assert status is ValidationStatus.FAILED
    assert "503" in finding


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: object) -> object:
        raise AssertionError("must not be called when no organization URL is configured")


async def test_missing_organization_url_raises_rather_than_reporting_a_status() -> None:
    """An unconfigured tenant is a configuration gap, not a real check outcome — the engine turns
    this into UNREACHABLE (FR-015), and no token should even be requested for it."""
    from groundwork_controlplane.validation.engine import ValidationContext

    context = ValidationContext(
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id="33333333-3333-3333-3333-333333333333",
        region="australiaeast",
        vnet_address_space="10.0.0.0/16",
        deployment_identity_object_id=PRINCIPAL_ID,
        credential=_FakeCredential(),  # type: ignore[arg-type]
        devops_organization_url=None,
    )

    with pytest.raises(ValueError, match="no Azure DevOps organization URL"):
        await organization_reachable(context)
