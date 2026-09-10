"""Report content assembly (T092; FR-034, FR-052, SC-015).

Assembles everything a :class:`~groundwork_contracts.audit.DeploymentReport` needs to say about a
*finished* deployment — the authority chain, per-stage outcomes (including which stages never ran,
SC-015's own requirement), resources created, and final cost — from durable state alone
(``Deployment`` plus its append-only ``DeploymentStageRecord`` history), the same
reconstruct-from-Cosmos discipline ``engine/halt.py`` already established for halt reasons.

Deliberately returns :class:`ReportContent`, not a complete
:class:`~groundwork_contracts.audit.DeploymentReport` — a report's own storage identity
(``report_id``, ``blob_uri``, ``content_hash``, ``retention_expires_at``) is ``state/report_archive.
py``'s concern (T093), not this module's. Splitting it this way means the assembly logic here is
testable with no blob client at all, the same reasoning every other builder/store split in this
codebase already follows (``approval/service.py`` vs. ``approval/artefacts.py``,
``engine/preview.py``'s ``WhatIfCapture`` vs. ``WhatIfPreviewStore``).
"""

from __future__ import annotations

from dataclasses import dataclass

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentStageRecord,
    ReportOutcome,
    StageSummary,
)
from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import DeploymentPlan
from groundwork_orchestrator.engine.halt import load_all_stage_records
from groundwork_orchestrator.state.repositories import StageRecordRepository

_REPORTABLE_OUTCOMES: dict[DeploymentStatus, ReportOutcome] = {
    DeploymentStatus.SUCCEEDED: ReportOutcome.SUCCEEDED,
    DeploymentStatus.HALTED: ReportOutcome.HALTED,
    DeploymentStatus.ROLLED_BACK: ReportOutcome.ROLLED_BACK,
}


class NotReportableError(Exception):
    """Raised for a deployment not yet in a terminal, reportable state (``QUEUED``/``EXECUTING``).

    A caller reaching this has a real bug — report generation is only ever triggered from
    ``Sequencer``'s own terminal-state transitions (``_mark_succeeded``, ``_mark_halted``), which by
    construction only run once ``deployment.status`` is already one of the three reportable values.
    """


@dataclass(frozen=True, slots=True)
class ReportContent:
    """Everything :class:`~groundwork_contracts.audit.DeploymentReport` needs except its own
    storage identity."""

    deployment_id: str
    tenant_id: str
    correlation_id: str
    authority: AuthorityChain
    outcome: ReportOutcome
    stage_summary: tuple[StageSummary, ...]
    resources_created: tuple[str, ...]
    iac_artefact_versions: dict[str, str]
    final_monthly_cost_aud: float


def _outcome_for(status: DeploymentStatus) -> ReportOutcome:
    outcome = _REPORTABLE_OUTCOMES.get(status)
    if outcome is None:
        raise NotReportableError(
            f"deployment status {status.value!r} is not a reportable terminal state; only "
            f"{[s.value for s in _REPORTABLE_OUTCOMES]} are"
        )
    return outcome


def _stage_summary(
    stage_records: list[DeploymentStageRecord], *, execution_order: tuple[str, ...]
) -> tuple[tuple[StageSummary, ...], tuple[str, ...]]:
    """Per-stage outcome for every real blueprint stage, plus the union of resources any of them
    reported affecting. Pseudo-stages (``what_if_preview``, ``policy_preflight``,
    ``cost_reapproval``) are deliberately excluded — ``execution_order`` only ever names real
    blueprint stages, and SC-015's "which stages succeeded, failed, never ran" is about the
    customer's platform being built, not this system's own internal preflight machinery."""
    latest_by_stage: dict[str, DeploymentStageRecord] = {}
    attempts_by_stage: dict[str, int] = {}
    for record in stage_records:
        attempts_by_stage[record.stage_name] = attempts_by_stage.get(record.stage_name, 0) + 1
        existing = latest_by_stage.get(record.stage_name)
        if existing is None or record.attempt > existing.attempt:
            latest_by_stage[record.stage_name] = record

    summaries: list[StageSummary] = []
    resources: set[str] = set()
    for stage_name in execution_order:
        record = latest_by_stage.get(stage_name)
        if record is None:
            summaries.append(StageSummary(stage_name=stage_name, never_ran=True, attempts=0))
            continue
        summaries.append(
            StageSummary(
                stage_name=stage_name,
                status=record.status,
                attempts=attempts_by_stage[stage_name],
            )
        )
        resources.update(record.resources_affected)
    return tuple(summaries), tuple(sorted(resources))


async def build_report_content(
    deployment: Deployment,
    plan: DeploymentPlan,
    *,
    stage_record_repository: StageRecordRepository,
    blueprint: PlatformBlueprint,
) -> ReportContent:
    """Assemble a finished deployment's report content from durable state alone.

    ``final_monthly_cost_aud`` is read from ``plan.cost_estimate.monthly_total`` — the approved
    figure, not a freshly re-priced one. **Disclosed limitation**: if T075a's cost re-approval fired
    during execution, the plan's own estimate no longer reflects the figure actually authorised
    (that lives on the fresh ``Approval`` the orchestrator has no access to — the
    deterministic-execution boundary forbids it importing ``groundwork_controlplane``). Correcting
    this would need the re-approved figure
    threaded back onto ``Deployment`` or ``DeploymentPlan`` at recovery time, which nothing does
    today; not attempted here rather than silently guessed at.
    """
    outcome = _outcome_for(deployment.status)
    stage_records = await load_all_stage_records(
        stage_record_repository, deployment.tenant_id, deployment.deployment_id
    )
    stage_summary, resources_created = _stage_summary(
        stage_records, execution_order=blueprint.execution_order()
    )

    return ReportContent(
        deployment_id=deployment.deployment_id,
        tenant_id=deployment.tenant_id,
        correlation_id=deployment.correlation_id,
        authority=deployment.authority,
        outcome=outcome,
        stage_summary=stage_summary,
        resources_created=resources_created,
        iac_artefact_versions={blueprint.blueprint_id: blueprint.version},
        final_monthly_cost_aud=plan.cost_estimate.monthly_total,
    )
