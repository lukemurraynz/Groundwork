"""Halt-and-preserve reconstruction (T073; FR-031, FR-031a, FR-031b).

``Sequencer.run`` (``engine/sequencer.py``) already halts on a permanent failure and never tears
anything down automatically — that guarantee has existed since T070. What this module adds is the
missing other half of FR-031: the ability for anything *other* than the sequencer call that just
halted — a later GET status request, a different replica, the recovery endpoint
(``api/recovery.py``, T085) — to durably reconstruct which stage failed, why, and what a human may
choose, purely from what is already persisted (``Deployment`` plus its append-only
``DeploymentStageRecord`` history).
Nothing new is written to Cosmos to support this; ``Sequencer._mark_halted`` intentionally does not
grow a new field.

**Why the failing stage can be reconstructed instead of stored.** ``Sequencer.run`` halts
immediately and returns as soon as a stage's outcome is a permanent (non-retryable, or
budget-exhausted) ``FAILED`` — no further stage records are written for that deployment on that
call, and no other call is running concurrently against it (the subscription lease guarantees that,
FR-045b). So for a deployment whose current ``status`` is ``HALTED``, the most recently-ended
:class:`~groundwork_contracts.audit.DeploymentStageRecord` across *every* stage it has ever
attempted is, by construction, exactly the record that caused the halt. This is cheaper and more
robust than persisting a redundant copy of the same fact on ``Deployment`` itself, which would need
its own consistency discipline (what happens if the two disagree after a crash mid-write?) for no
benefit.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from groundwork_contracts.audit import DeploymentStageRecord, StageError, StageStatus
from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_orchestrator.state.repositories import StageRecordRepository

RECOVERY_ACTIONS = ("retry", "forward_fix", "rollback")

_RECOVERY_ACTION_HELP: dict[str, str] = {
    "retry": "Resume from the last completed stage and try the same plan again.",
    "forward_fix": "Resume from the last completed stage after you've addressed the underlying "
    + "issue (e.g. a quota or permission problem) outside Groundwork.",
    "rollback": "Redeploy the last known-good configuration through your own Azure DevOps "
    + "pipeline. This is a whole-stack redeploy to a prior state, not a teardown, and requires "
    + "its own separate approval.",
}


def recovery_action_help(action: str) -> str:
    """Plain-language explanation of one recovery action, for customer-facing surfaces.

    ``recovery_options_for_stage`` already tells a caller *which* actions are available; this
    tells them *what each one does* — added because ``rollback`` in particular is easy to
    misread as "undo everything" when it is actually a redeploy-to-last-known-good
    (``docs/adr/0009-proposal-rollback-via-customer-pipeline.md``).
    """
    return _RECOVERY_ACTION_HELP.get(action, "")


_PSEUDO_STAGE_RECOVERY_OPTIONS: dict[str, tuple[str, ...]] = {
    # T074: nothing has been written to the customer tenant by a preview capture — no rollback.
    "what_if_preview": ("retry", "forward_fix"),
    # T075: a policy re-check found (or couldn't confirm the absence of) a blocking assignment —
    # nothing this platform manages was written; no rollback.
    "policy_preflight": ("retry", "forward_fix"),
    # T075a: a cost overrun can only be resolved by a genuinely new human decision (a fresh
    # Approval, gated by api/recovery.py exactly like rollback's own approvalId requirement) — not
    # by "forward-fixing" anything, so forward_fix is deliberately not offered here.
    "cost_reapproval": ("retry",),
    # T021e: a revoked/pending consent halt can only be resolved by the customer re-consenting
    # (POST .../onboarding/confirm) — same reasoning as cost_reapproval, forward_fix has nothing
    # to forward-fix.
    "consent_check": ("retry",),
    # FR-038b: same reasoning as consent_check — resolved by the customer granting Azure DevOps
    # org access and an operator confirming it (POST .../onboarding/ado-access-confirm).
    "ado_org_access_check": ("retry",),
}


def recovery_options_for_stage(blueprint: PlatformBlueprint, stage_name: str) -> tuple[str, ...]:
    """The recovery actions FR-031 permits for one stage's failure.

    Retry and forward-fix are always offered for a real blueprint stage; rollback only where the
    blueprint's own ``recovery_path`` prose names it — some stages (Fabric capacity, billable to
    the customer and not safely reversible) declare halt-and-preserve as the *only* correct
    response. The single source of truth for this decision: both ``Sequencer`` (constructing a
    fresh ``HaltedState`` right after halting) and ``api/recovery.py`` (validating a customer's
    later choice against it) call this same function rather than each encoding the rule
    separately — which is exactly why a synthetic pseudo-stage (a name absent from the blueprint,
    used by T074/T075/T075a for checks that run before any real stage) needs its own explicit
    entry here rather than falling through to the generic ``(retry, forward_fix)`` default: that
    default is wrong for ``cost_reapproval`` specifically, and silently reusing it would let
    ``api/recovery.py`` offer an action nothing downstream is prepared to gate correctly.
    """
    stage = next((s for s in blueprint.stages if s.name == stage_name), None)
    if stage is None:
        return _PSEUDO_STAGE_RECOVERY_OPTIONS.get(stage_name, ("retry", "forward_fix"))
    options = ["retry", "forward_fix"]
    if "rollback" in stage.recovery_path.lower():
        options.append("rollback")
    return tuple(options)


@dataclass(frozen=True, slots=True)
class HaltedView:
    """What a human needs to decide a halted deployment's next step (FR-031)."""

    failing_stage: str
    error: StageError
    recovery_options: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class StageStatusView:
    """One row of the ``GET /deployments/{deploymentId}`` status monitor's ``stages`` array."""

    stage_name: str
    status: str
    idempotence_outcome: str | None
    attempt: int


async def load_all_stage_records(
    stage_record_repository: StageRecordRepository, tenant_id: str, deployment_id: str
) -> list[DeploymentStageRecord]:
    """Every attempt of every stage this deployment has ever made — unlike
    ``engine.retry.load_stage_attempt_history``, not filtered to a single stage name, since
    reconstructing the full halt-and-preserve picture needs the whole history."""
    query = "SELECT * FROM c WHERE c.deployment_id = @deployment_id"
    parameters = [{"name": "@deployment_id", "value": deployment_id}]
    return [record async for record in stage_record_repository.query(tenant_id, query, parameters)]


def reconstruct_halted_view(
    stage_records: Sequence[DeploymentStageRecord], blueprint: PlatformBlueprint
) -> HaltedView | None:
    """``None`` if ``stage_records`` carries no failure to report — the caller is expected to only
    treat the result as meaningful when the deployment's own ``status`` is ``HALTED`` (see module
    docstring for why the most-recently-ended record is guaranteed to be the halting one in that
    case)."""
    if not stage_records:
        return None
    latest = max(stage_records, key=lambda r: r.ended_at or r.started_at)
    if latest.status is not StageStatus.FAILED or latest.error is None:
        return None
    return HaltedView(
        failing_stage=latest.stage_name,
        error=latest.error,
        recovery_options=recovery_options_for_stage(blueprint, latest.stage_name),
    )


def build_stage_status_view(
    stage_records: Sequence[DeploymentStageRecord],
    *,
    execution_order: Sequence[str],
    current_stage: str | None,
) -> list[StageStatusView]:
    """The ``stages`` array for the status monitor (``control-plane-api.md``'s own worked example):
    one entry per stage that has either completed at least one attempt, or is genuinely in flight
    right now. A stage neither attempted nor currently running is omitted entirely, not reported
    with an invented "pending" status this module has no evidence for.

    ``current_stage`` is checked *before* any existing record for that stage, not only when no
    record exists: a stage can genuinely be mid-retry — one or more prior attempts already recorded
    ``FAILED``, and a fresh attempt now in flight with no record of its own yet (the window between
    ``_run_one_stage`` marking ``current_stage`` and that attempt's own record being written). In
    that window the most recent *record* is stale (it describes the attempt *before* this one), so
    reporting it as this stage's current status would understate both the attempt number and how
    far the deployment has actually progressed.
    """
    latest_by_stage: dict[str, DeploymentStageRecord] = {}
    attempts_by_stage: dict[str, int] = {}
    for record in stage_records:
        attempts_by_stage[record.stage_name] = attempts_by_stage.get(record.stage_name, 0) + 1
        existing = latest_by_stage.get(record.stage_name)
        if existing is None or record.attempt > existing.attempt:
            latest_by_stage[record.stage_name] = record

    views: list[StageStatusView] = []
    for stage_name in execution_order:
        if stage_name == current_stage:
            views.append(
                StageStatusView(
                    stage_name=stage_name,
                    status=StageStatus.STARTED.value,
                    idempotence_outcome=None,
                    attempt=attempts_by_stage.get(stage_name, 0) + 1,
                )
            )
            continue
        record = latest_by_stage.get(stage_name)
        if record is not None:
            views.append(
                StageStatusView(
                    stage_name=stage_name,
                    status=record.status.value,
                    idempotence_outcome=(
                        record.idempotence_outcome.value if record.idempotence_outcome else None
                    ),
                    attempt=record.attempt,
                )
            )
    return views


def requeue_after_recovery_choice(deployment: Deployment) -> Deployment:
    """The field-clearing a deployment needs to safely become ``QUEUED`` again, so the
    queue-consumption loop's next poll resumes it from the last checkpoint exactly as any other
    resume does — matching the API contract's own description of ``retry``/``forward_fix``:
    "resume from the last checkpoint without re-running completed stages" (FR-030). The same field
    set ``Sequencer._mark_requeued`` clears for an *automatic* transient-failure requeue, extracted
    here so ``api/recovery.py`` can reuse it for a *human-chosen* one without needing a full
    ``Sequencer`` instance — one rule for "what does a safe requeue clear", not two.

    ``completed_at`` must be cleared too, not just ``current_stage``/``started_at``/``lease``:
    ``Deployment``'s own ``_terminal_and_active_states_match_timestamps`` validator requires a
    ``queued`` deployment to have no ``completed_at``. A requeue originating from ``executing``
    (T072's automatic retry) never had ``completed_at`` set in the first place, which is why this
    was never exercised until a requeue from ``halted`` (this function's other caller,
    ``api/recovery.py`` — a halted deployment always has ``completed_at`` set) started using the
    same function and a test caught it (this codebase's established discipline: ``model_copy``
    does not re-run validators, so an incomplete clear only fails on the next read-back, not here).

    Returns the updated model only; persisting it is the caller's job (this module has no
    repository dependency of its own for writes, only for the read-side reconstruction above).
    """
    return deployment.model_copy(
        update={
            "status": DeploymentStatus.QUEUED,
            "current_stage": None,
            "started_at": None,
            "completed_at": None,
            "lease": None,
        }
    )
