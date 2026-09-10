"""Execution-time cost re-check with re-approval and escalation (T075a; FR-019).

Re-runs the exact live pricing computation the plan-time estimate used
(``groundwork_shared.costing`` — moved there 2026-08-02 from
``groundwork_controlplane.costing`` specifically so this module could call it directly rather than
duplicate it a second time the way ``engine/preflight.py``'s policy re-check had to duplicate
``validation/checks/policy.py``: retail pricing has no control-plane-only dependency — no
authenticated Azure client, no FastAPI app state — so extraction was the cheaper, more correct
move once a second orchestrator-side consumer existed) immediately before the ``infrastructure``
stage, the same point ``engine/preflight.py``'s policy re-check already established for "must
still be true right before we start spending money" checks.

**Design, decided directly by the product owner (2026-08-02), not guessed at:** a cost re-check
that finds the recomputed monthly total outside the *originally approved estimate's own
uncertainty band* halts the deployment as a synthetic ``cost_reapproval`` pseudo-stage — the same
pattern T074's ``what_if_preview`` established — rather than inventing a new ``DeploymentStatus``
value. Unlike every other halt reason in this codebase, this one can never be resolved by a bare
"retry": FR-019 requires an actual new human decision (a fresh ``Approval``), so
``api/recovery.py`` gates retry for this specific failing stage behind an ``approvalId`` exactly
the way it already gates ``rollback`` (T085) — see that module's own handling. Where the
recomputed figure also crosses the FR-020a second-approver threshold, the fresh approval supplied
must itself carry a ``second_approval`` — a single self-approval cannot satisfy an escalated
re-approval, matching SC-018's zero-tolerance for one identity satisfying both roles, now applied
to the *escalation* case too, not just the original approval.

**A recheck that lands inside the band is not reported at all** — FR-019 is explicit that this
must not fire on pricing noise: "A figure inside the band was already accepted by the approver and
MUST NOT trigger re-approval." The sequencer proceeds straight to ``infrastructure`` with no record
of the recheck beyond ordinary telemetry, exactly as a passing policy preflight leaves no
``DeploymentStageRecord`` of its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from groundwork_contracts.plan import DeploymentPlan
from groundwork_shared.config.settings import GovernanceSettings
from groundwork_shared.costing.estimator import compose_estimate, fabric_capacity_line
from groundwork_shared.costing.retail_prices import RetailPricesClient

COST_REAPPROVAL_PSEUDO_STAGE = "cost_reapproval"


class CostPreflightError(Exception):
    """The recheck itself could not be completed (Retail Prices API unreachable, pricing
    disagreement) — distinct from "the check ran and the figure moved," which is a normal,
    fully-reported result."""


@dataclass(frozen=True, slots=True)
class CostRecheckResult:
    """What FR-019 needs to decide: is the deployment still within the approved band, and if not,
    does the new figure also cross the second-approver threshold."""

    within_band: bool
    recomputed_monthly_total: float
    approved_monthly_total: float
    band_lower: float
    band_upper: float
    crosses_second_approver_threshold: bool

    @property
    def detail(self) -> str:
        return (
            f"recomputed monthly total {self.recomputed_monthly_total} AUD is outside the "
            f"approved estimate's own uncertainty band [{self.band_lower}, {self.band_upper}] "
            f"AUD (approved figure was {self.approved_monthly_total} AUD)"
        )


def _approved_band(plan: DeploymentPlan) -> tuple[float, float]:
    ref = plan.cost_estimate
    lower = ref.monthly_total * (1 - ref.uncertainty_lower_pct / 100)
    upper = ref.monthly_total * (1 + ref.uncertainty_upper_pct / 100)
    return lower, upper


async def recheck_cost(
    *,
    plan: DeploymentPlan,
    retail_prices_client: RetailPricesClient,
    governance: GovernanceSettings,
    now: datetime,
) -> CostRecheckResult:
    """FR-019: re-verify the approved cost estimate against a fresh, live-priced computation.

    Raises :class:`CostPreflightError` if the recheck itself cannot complete — the sequencer
    converts that into a halt exactly like a raising stage, the same shape
    ``engine/preflight.py``'s policy check already uses.
    """
    try:
        fabric_line = await fabric_capacity_line(
            retail_prices_client, plan.fabric_capacity_sku, plan.region.value
        )
        recomputed = compose_estimate(fabric_line, now=now)
    except Exception as exc:
        raise CostPreflightError(
            f"cost preflight could not be completed: {type(exc).__name__}: {exc}"
        ) from exc

    lower, upper = _approved_band(plan)
    within_band = lower <= recomputed.monthly_total <= upper
    crosses_threshold = governance.requires_second_approver(
        recomputed.monthly_total, plan.environment.value
    )

    return CostRecheckResult(
        within_band=within_band,
        recomputed_monthly_total=recomputed.monthly_total,
        approved_monthly_total=plan.cost_estimate.monthly_total,
        band_lower=round(lower, 2),
        band_upper=round(upper, 2),
        crosses_second_approver_threshold=crosses_threshold,
    )


class CostPreflight:
    """Binds a real ``RetailPricesClient`` and ``GovernanceSettings`` so ``Sequencer`` can call
    this as a plain ``CostPreflightCheck`` (``__call__(plan, now=...)``) without knowing either
    dependency exists — the same injectable-seam-plus-real-implementation split every other
    Azure-calling piece of this codebase uses (``WhatIfCapture``, the policy preflight's own
    ``PolicyClient`` wrapping)."""

    def __init__(
        self, *, retail_prices_client: RetailPricesClient, governance: GovernanceSettings
    ) -> None:
        self._retail_prices_client = retail_prices_client
        self._governance = governance

    async def __call__(self, plan: DeploymentPlan, *, now: datetime) -> CostRecheckResult:
        return await recheck_cost(
            plan=plan,
            retail_prices_client=self._retail_prices_client,
            governance=self._governance,
            now=now,
        )
