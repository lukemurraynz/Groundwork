"""T075 — policy preflight re-check, unit-tested in isolation (pure logic) and wired through
the sequencer (preflight interceptor).

The pure logic functions are tested directly; the Azure-calling
``recheck_no_denying_resource_type_policy`` is tested via the sequencer's injectable
``PreflightCheck`` protocol rather than with a real ``PolicyClient`` (same discipline as every
other real-Azure-adjacent module this session fakes at its own boundary).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from groundwork_contracts.readiness import ValidationStatus
from groundwork_orchestrator.engine.preflight import (
    _applies_to_region,
    _definition_name_and_scope,
    _effective_effect,
    _evaluate_deny_effects,
    _is_enforced,
    _is_single_policy_definition,
    _rule_cannot_fire_for_region,
)


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (None, True),
        ("Default", True),
        ("DoNotEnforce", False),
        ("", False),
    ],
)
def test_is_enforced(mode: str | None, expected: bool) -> None:
    assert _is_enforced(mode) is expected


@pytest.mark.parametrize(
    ("policy_id", "expected"),
    [
        ("/providers/Microsoft.Authorization/policyDefinitions/test", True),
        ("/subscriptions/sub/providers/Microsoft.Authorization/policyDefinitions/test", True),
        ("/providers/Microsoft.Authorization/policySetDefinitions/test", False),
        ("/subscriptions/sub/providers/Microsoft.Authorization/policySetDefinitions/test", False),
    ],
)
def test_is_single_policy_definition(policy_id: str, expected: bool) -> None:
    assert _is_single_policy_definition(policy_id) is expected


def test_definition_name_and_scope_built_in() -> None:
    name, is_built_in = _definition_name_and_scope(
        "/providers/Microsoft.Authorization/policyDefinitions/RequireTag"
    )
    assert name == "RequireTag"
    assert is_built_in is True


def test_definition_name_and_scope_custom() -> None:
    name, is_built_in = _definition_name_and_scope(
        "/subscriptions/11111111-1111-1111-1111-111111111111/providers/"
        "Microsoft.Authorization/policyDefinitions/my-custom-policy"
    )
    assert name == "my-custom-policy"
    assert is_built_in is False


def test_evaluate_deny_effects_failed_when_assignments_found() -> None:
    status, detail = _evaluate_deny_effects(["Block resource creation", "Deny public IPs"])
    assert status is ValidationStatus.FAILED
    assert "2 enforced Deny-effect" in detail
    assert "Block resource creation" in detail
    assert "Deny public IPs" in detail


def test_evaluate_deny_effects_passed_when_none_found() -> None:
    status, detail = _evaluate_deny_effects([])
    assert status is ValidationStatus.PASSED
    assert "no enforced Deny-effect" in detail


# --- Region-awareness gates (added 2026-08-25, found live against the tenant-root
# sys.blockwesteurope SDP guardrail) -----------------------------------------------


def _sdp_guardrail_properties() -> SimpleNamespace:
    """The exact shape observed live: one resourceLocation selector whose 'in' list covers
    every SDP region except westeurope, plus a single policyEffect override to deny."""
    return SimpleNamespace(
        overrides=[SimpleNamespace(kind="policyEffect", value="deny")],
        resource_selectors=[
            SimpleNamespace(
                selectors=[
                    SimpleNamespace(
                        kind="resourceLocation",
                        in_=["australiaeast", "northeurope", "eastus2", "westus3"],
                    )
                ]
            )
        ],
    )


WE_GUARDRAIL_RULE = {
    "if": {"field": "location", "equals": "westeurope"},
    "then": {"effect": "deny"},
}


def test_effective_effect_override_replaces_static() -> None:
    rule = {"if": {}, "then": {"effect": "audit"}}
    assert _effective_effect(_sdp_guardrail_properties(), rule) == "deny"


def test_effective_effect_falls_back_to_static() -> None:
    rule = {"if": {}, "then": {"effect": "Deny"}}
    assert _effective_effect(SimpleNamespace(overrides=[]), rule) == "deny"


def test_applies_to_region_true_when_region_in_selector() -> None:
    assert _applies_to_region(_sdp_guardrail_properties(), "australiaeast") is True


def test_applies_to_region_false_when_region_excluded() -> None:
    assert _applies_to_region(_sdp_guardrail_properties(), "westeurope") is False


def test_applies_to_region_true_without_selectors() -> None:
    # No selectors at all: assignment applies everywhere (conservative default).
    assert _applies_to_region(SimpleNamespace(resource_selectors=[]), "westeurope") is True


def test_rule_cannot_fire_when_region_outside_equals() -> None:
    assert _rule_cannot_fire_for_region(WE_GUARDRAIL_RULE, "australiaeast") is True
    assert _rule_cannot_fire_for_region(WE_GUARDRAIL_RULE, "WestEurope") is False


def test_rule_cannot_fire_in_list_shape() -> None:
    rule = {"if": {"field": "location", "in": ["westeurope", "germanynorth"]}}
    assert _rule_cannot_fire_for_region(rule, "australiaeast") is True
    assert _rule_cannot_fire_for_region(rule, "westeurope") is False


@pytest.mark.parametrize(
    "rule",
    [
        {"if": {"field": "type", "equals": "Microsoft.Storage/storageAccounts"}},
        {"if": {"allOf": [{"field": "location", "equals": "westeurope"}]}},
        {},
    ],
)
def test_rule_unrecognised_shapes_are_conservative(rule: dict[str, object]) -> None:
    assert _rule_cannot_fire_for_region(rule, "australiaeast") is False


def test_full_gate_combination_dormant_guardrail_is_not_flagged() -> None:
    """The exact live false positive: australiaeast deployment vs the dormant WE guardrail must
    pass all three gates (override deny applies, selector keeps region in scope, and the rule
    provably cannot fire for this region) — which together mean the assignment is skipped."""
    properties = _sdp_guardrail_properties()
    assert _effective_effect(properties, WE_GUARDRAIL_RULE) == "deny"
    assert _applies_to_region(properties, "australiaeast")
    assert _rule_cannot_fire_for_region(WE_GUARDRAIL_RULE, "australiaeast")


def test_full_gate_combination_genuine_block_still_flagged() -> None:
    """A deny rule with no location condition on an unscoped assignment keeps being flagged."""
    properties = SimpleNamespace(overrides=[], resource_selectors=[])
    rule = {"if": {"field": "tags.costCentre", "equals": ""}, "then": {"effect": "deny"}}
    assert _effective_effect(properties, rule) == "deny"
    assert _applies_to_region(properties, "australiaeast")
    assert not _rule_cannot_fire_for_region(rule, "australiaeast")
