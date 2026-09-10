"""Execution-time Azure Policy preflight re-check (T075; FR-014 assertion
``policy.no-denying-resource-type-policy`` re-evaluated immediately before the infrastructure
stage applies its deployment stack).

The plan-time readiness check (``groundwork_shared.validation.checks.policy``) already
verified this once, but Azure Policy assignments can change between plan time and execution time.
This re-check serves the same "validate before promise, then re-validate before write" discipline
as FR-019's cost re-check — it is not a second, independent design; it is the same assertion, the
same logic, run again at a different point in the lifecycle.

**Why duplicated rather than shared.** The orchestrator must never import
``groundwork_controlplane``. The pure decision functions (``_is_enforced``, etc.) have no
control-plane dependencies — they could live in ``groundwork_shared`` — but extracting them would
require changing the control-plane import path and the orchestrator would become the second
consumer of a shared module that originally had one. Duplicating ~65 lines of pure logic is
cheaper than the coordination cost of extracting now and potentially breaking a green check stack.
Same reasoning ``stages/infrastructure.py``'s own docstring records for
``deployment_resource_group_name``.

**Disclosed scope boundary** (same as the plan-time check's own): subscription-scope,
single-policy assignments only. Policy set (initiative) member-policy resolution and
resource-group-scope enumeration are not evaluated here — see the original module's docstring.

**Region-awareness (added 2026-08-25, found live):** an enforced Deny-effect assignment is only
flagged when it can actually fire for THIS deployment's target region. Three gates, each
conservative (unrecognised shapes keep the assignment flagged rather than silently skipping it):

1. ``resourceSelectors`` — an assignment whose ``resourceLocation`` ``in``-list excludes the
   target region is scoped away from it entirely and cannot deny its resources. This is exactly
   how Microsoft ships dormant regional guardrails (the tenant-root ``sys.blockwesteurope``
   assignment observed live: definition denies ``location == westeurope``, but its selector
   scopes the assignment to every region *except* West Europe, so it denies nothing anywhere).
2. Assignment-level ``policyEffect`` overrides replace the definition's static effect.
3. The rule's own ``if`` condition, for the simple location-equality shape only: a rule of
   ``{field: location, equals/in: X}`` cannot fire for a target region outside ``X``.

Complex rule shapes (nested expressions, non-location fields) are still flagged on static
Deny effect — over-reporting is acceptable, under-reporting is not.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.readiness import ValidationStatus

ASSERTION_ID = "policy.no-denying-resource-type-policy"

# Found live 2026-08-24: azure-mgmt-resource's async PolicyClient has no overall deadline on its
# own retry policy — a slow or stuck list()/get() call here hangs Sequencer.run() indefinitely,
# with no exception raised and nothing logged (the transition from devops_project into
# infrastructure is the only place this preflight runs, and it was silently stalling every real
# deployment attempt at exactly that point). A bound here converts that into the same
# PolicyPreflightError -> halted-with-retry path every other failure of this check already takes.
PREFLIGHT_TIMEOUT_SECONDS = 60.0

_ENFORCED_MODES = frozenset({None, "Default"})


def _is_enforced(enforcement_mode: str | None) -> bool:
    return enforcement_mode in _ENFORCED_MODES


def _is_single_policy_definition(policy_definition_id: str) -> bool:
    return "/policydefinitions/" in policy_definition_id.lower()


def _definition_name_and_scope(policy_definition_id: str) -> tuple[str, bool]:
    name = policy_definition_id.strip("/").split("/")[-1]
    is_built_in = "/subscriptions/" not in policy_definition_id.lower()
    return name, is_built_in


def _effective_effect(properties: object, rule: dict[str, Any]) -> str:
    """The assignment's effective Deny verdict: the definition's static ``then.effect``,
    replaced by any assignment-level ``policyEffect`` override.

    An override carrying its own nested selectors would apply the replacement only to the
    resources matching them; treating it as a wholesale replacement is deliberate here because
    the check's bias is over-reporting (a false halt is recoverable by a human, a missed block
    is a failed customer deployment). The live SDP-guardrail shape uses exactly one unscoped
    override.
    """
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
    Everything else returns ``False`` — the rule is assumed able to fire, so a genuine Deny
    keeps being flagged. This is what distinguishes Microsoft's dormant regional guardrails
    (definition denies ``westeurope`` only) from rules that would actually block the plan.
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


@dataclass(frozen=True, slots=True)
class PreflightContext:
    """Minimal context for an execution-time preflight check — the subset of the plan-time
    ``ValidationContext`` that the policy check actually reads. Duplicated here rather than
    imported (see module docstring)."""

    subscription_id: str
    credential: AsyncTokenCredential
    region: str


class PolicyPreflightError(Exception):
    """The preflight check could not be completed — not a policy violation (which is a normal,
    fully-reported result), but a genuine infrastructure failure that prevented the check from
    running at all (network error, auth failure, SDK exception)."""


async def recheck_no_denying_resource_type_policy(
    context: PreflightContext,
) -> tuple[ValidationStatus, str]:
    """Re-evaluate the plan-time policy assertion immediately before the infrastructure stage.

    Returns ``(PASSED, reason)`` or ``(FAILED, reason)`` on a normal, fully-reported result.
    Raises ``PolicyPreflightError`` if the check itself cannot run — a distinct failure mode
    from "the check ran and found a policy violation," matching the plan-time engine's own
    ``UNREACHABLE`` concept but surfaced as a Python exception (the sequencer converts it into
    a ``FAILED`` outcome exactly like a raising stage).
    """
    from azure.mgmt.resource.policy.aio import PolicyClient

    async def _collect() -> list[str]:
        deny_assignment_names: list[str] = []
        async with PolicyClient(context.credential, context.subscription_id) as client:
            async for assignment in client.policy_assignments.list():
                properties = assignment.properties
                if properties is None or not _is_enforced(properties.enforcement_mode):
                    continue
                policy_definition_id = properties.policy_definition_id or ""
                if not _is_single_policy_definition(policy_definition_id):
                    continue
                # Scoped away from this deployment's target region entirely: nothing it denies
                # can apply here. Checked before the definition fetch — an out-of-scope
                # assignment is skipped without spending the extra GET.
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
                # The assignment denies in general but provably not for this region's shape
                # (e.g. Microsoft's dormant "should not be created in West Europe" guardrail,
                # whose rule only fires on location == westeurope).
                if _rule_cannot_fire_for_region(rule, context.region):
                    continue
                deny_assignment_names.append(properties.display_name or assignment.name or name)
        return deny_assignment_names

    try:
        deny_assignment_names = await asyncio.wait_for(
            _collect(), timeout=PREFLIGHT_TIMEOUT_SECONDS
        )
    except TimeoutError as exc:
        raise PolicyPreflightError(
            f"policy preflight check did not complete within {PREFLIGHT_TIMEOUT_SECONDS}s"
        ) from exc
    except Exception as exc:
        raise PolicyPreflightError(
            f"policy preflight check could not be completed: {type(exc).__name__}: {exc}"
        ) from exc

    return _evaluate_deny_effects(deny_assignment_names)
