"""Azure Policy and governance assertions (T040).

Verified 2026-07-31 against the installed ``azure-mgmt-resource-policy`` SDK (Policy was split out
of ``azure-mgmt-resource`` into its own distribution at some point after the taxonomy this project's
earlier research assumed — ``azure.mgmt.resource.policy`` is a separate PyPI package, not a
submodule of ``azure-mgmt-resource``, and must be installed explicitly).

**Disclosed scope boundary**: this check evaluates single-policy assignments at subscription scope
only. It does not follow policy *set* (initiative) assignments to their member policies, and does
not evaluate resource-group-scope assignments. Both are real gaps, not oversights — initiative
member-policy resolution and cross-scope enumeration are materially harder problems (arbitrary
policy-set nesting, scope hierarchy walking) that deserve their own task rather than an
unacknowledged partial implementation inside this one. A subscription with a blocking Deny policy
assigned only via an initiative, or only at resource-group scope, will not be caught by this check
today — recorded here rather than silently overclaimed in the assertion text.

**Region-awareness (added 2026-08-25, found live — mirrors the orchestrator's execution-time
preflight, which is deliberately duplicated rather than imported, per the deterministic-execution
boundary):** an enforced
Deny-effect assignment is only flagged when it can actually fire for the plan's target region.
Three conservative gates: ``resourceSelectors`` scoping (a ``resourceLocation`` ``in``-list that
excludes the target region scopes the whole assignment away), assignment-level ``policyEffect``
overrides replacing the static effect, and the rule's simple location-equality shape (Microsoft's
dormant "should not be created in West Europe" guardrail denies only ``location == westeurope``,
which cannot fire for any other region). Complex rule shapes keep being flagged on static Deny
effect — over-reporting is acceptable, under-reporting is not.
"""

from __future__ import annotations

from typing import Any

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "policy.no-denying-resource-type-policy"

_ENFORCED_MODES = frozenset({None, "Default"})


def _is_enforced(enforcement_mode: str | None) -> bool:
    return enforcement_mode in _ENFORCED_MODES


def _is_single_policy_definition(policy_definition_id: str) -> bool:
    """Whether ``policy_definition_id`` names a single policy, not a policy set (initiative).

    Policy set assignments point at a ``policySetDefinitions`` resource instead; those are the
    documented scope boundary above, not evaluated here.
    """
    return "/policydefinitions/" in policy_definition_id.lower()


def _definition_name_and_scope(policy_definition_id: str) -> tuple[str, bool]:
    """Return (definition name, is_built_in). Built-in definitions have no subscription prefix."""
    name = policy_definition_id.strip("/").split("/")[-1]
    is_built_in = "/subscriptions/" not in policy_definition_id.lower()
    return name, is_built_in


def _effective_effect(properties: object, rule: dict[str, Any]) -> str:
    """The assignment's effective effect: static ``then.effect``, replaced by any assignment-level
    ``policyEffect`` override. Overrides with nested selectors are treated as wholesale
    replacements — deliberate over-reporting bias; see module docstring."""
    effect = str(rule.get("then", {}).get("effect", "")).lower()
    for override in getattr(properties, "overrides", None) or []:
        if str(getattr(override, "kind", "")).lower().replace("_", "") == "policyeffect":
            value = str(getattr(override, "value", "")).lower()
            if value:
                return value
    return effect


def _applies_to_region(properties: object, region: str) -> bool:
    """Whether the assignment's own ``resourceSelectors`` leave the target region in scope.

    Azure evaluates a resourceSelector as a filter: the assignment affects only resources
    matching ALL of them. A ``resourceLocation`` ``in``-list that omits the deployment's target
    region therefore scopes the whole assignment away from this deployment. Selectors of other
    kinds are ignored — assumed to apply, keeping the check conservative.
    """
    for resource_selector in getattr(properties, "resource_selectors", None) or []:
        for selector in getattr(resource_selector, "selectors", None) or []:
            kind = str(getattr(selector, "kind", "")).lower().replace("_", "")
            included = getattr(selector, "in_", None)
            if not included:
                continue
            included_regions = [str(x).lower() for x in included]
            if kind == "resourcelocation" and region.lower() not in included_regions:
                return False
    return True


def _rule_cannot_fire_for_region(rule: dict[str, Any], region: str) -> bool:
    """Whether the definition's rule provably cannot fire for the target region.

    Recognised: the simple single-condition shape ``{if: {field: location, equals|in: X}}``.
    Everything else returns ``False`` — assumed able to fire, so a genuine Deny keeps being
    flagged.
    """
    condition = rule.get("if")
    if not isinstance(condition, dict):
        return False
    if str(condition.get("field", "")).lower() != "location":
        return False
    if "equals" in condition:
        denied_locations = {str(condition["equals"]).lower()}
    elif isinstance(condition.get("in"), list):
        denied_locations = {str(x).lower() for x in condition["in"]}
    else:
        return False
    return region.lower() not in denied_locations


def _evaluate_deny_effects(deny_assignment_names: list[str]) -> tuple[ValidationStatus, str]:
    if deny_assignment_names:
        return (
            ValidationStatus.FAILED,
            f"{len(deny_assignment_names)} enforced Deny-effect policy assignment(s) found: "
            f"{', '.join(sorted(deny_assignment_names))}",
        )
    return (
        ValidationStatus.PASSED,
        "no enforced Deny-effect single-policy assignment found at subscription scope",
    )


async def no_denying_resource_type_policy(
    context: ValidationContext,
) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``policy.no-denying-resource-type-policy``.

    See the module docstring for the disclosed scope boundary — subscription-scope, single-policy
    assignments only.
    """
    from azure.mgmt.resource.policy.aio import PolicyClient

    deny_assignment_names: list[str] = []
    async with PolicyClient(context.credential, context.subscription_id) as client:
        async for assignment in client.policy_assignments.list():
            properties = assignment.properties
            if properties is None or not _is_enforced(properties.enforcement_mode):
                continue
            policy_definition_id = properties.policy_definition_id or ""
            if not _is_single_policy_definition(policy_definition_id):
                continue
            # Scoped away from this plan's target region entirely: nothing it denies can apply
            # here. Checked before the definition fetch — an out-of-scope assignment is skipped
            # without spending the extra GET.
            if not _applies_to_region(properties, context.region):
                continue

            name, is_built_in = _definition_name_and_scope(policy_definition_id)
            definition = (
                await client.policy_definitions.get_built_in(name)
                if is_built_in
                else await client.policy_definitions.get(name)
            )
            rule = (definition.properties.policy_rule if definition.properties else None) or {}
            if _effective_effect(properties, rule) != "deny":
                continue
            # Denies in general but provably not for this region's shape — e.g. Microsoft's
            # dormant "should not be created in West Europe" guardrail.
            if _rule_cannot_fire_for_region(rule, context.region):
                continue
            deny_assignment_names.append(properties.display_name or assignment.name or name)

    return _evaluate_deny_effects(deny_assignment_names)
