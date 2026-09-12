"""The stage-sequencing engine (T070; FR-027, FR-028, FR-029, FR-047, audit-by-construction).

Executes a :class:`~groundwork_contracts.blueprint.PlatformBlueprint`'s declared stage order
(``execution_order()``, already dependency-resolved and cycle-checked at blueprint load time)
against one :class:`~groundwork_contracts.deployment.Deployment`. Three guarantees this module
exists to make structural:

1. **Audit before action (the audit-by-construction rule, FR-047).** Every stage attempt's
   :class:`~groundwork_contracts.audit.AuditRecord` is durably committed *before* the stage's
   ``execute`` is ever called — the same ``AuditRepository.record_before`` ordering already used
   elsewhere, applied here to stage execution specifically.
2. **Every declared stage is executable (FR-028).** :class:`Sequencer` refuses to construct if the
   blueprint names a stage with no registered :class:`Stage` implementation — the same
   fail-at-construction discipline as ``ReadinessEngine`` refusing an assertion with no check.
3. **Halt and preserve, never silent partial success (FR-031, FR-034).** A stage that returns
   ``FAILED`` stops the run immediately. Completed stages are not touched, and nothing here ever
   tears anything down automatically — the deployment moves to ``HALTED`` and stays there until a
   human chooses retry, forward-fix, or rollback (``api/recovery.py``).
4. **A stage that raises halts exactly like one that returns FAILED.** Real Azure calls fail in
   ways a stage author cannot enumerate in advance — a network error, an unexpected response
   shape, an expired token. ``_run_one_stage`` catches anything a ``Stage.execute`` raises and
   converts it into a ``FAILED`` outcome (secret-scrubbed message, ``is_transient=False`` until
   T072's real classification exists) rather than letting it propagate uncaught and leave the
   deployment stuck ``EXECUTING`` with no record of why — the same reasoning as
   ``ReadinessEngine`` turning a raised check into ``UNREACHABLE`` instead of an unhandled crash.
5. **A stage that hangs is bounded, not just one that raises (waf-assessment.md §1.2).** A caught
   exception and an indefinite hang are different failure modes — the try/except above does
   nothing for a call that simply never returns. ``_run_one_stage`` wraps ``stage.execute()`` in
   ``asyncio.wait_for`` using the blueprint's own ``BlueprintStage.timeout_seconds`` (default
   1800s), converting a timeout into the same transient ``FAILED`` outcome path a raised
   transient exception already takes, rather than orphaning the deployment until its subscription
   lease expires naturally.

**Retry (T072, FR-030) and halt reconstruction (T073, FR-031) are both now live.** A transient
failure, or one whose blueprint-declared minimum retry interval has not yet elapsed, requeues the
deployment rather than halting it (``engine/retry.py``). A permanent failure halts exactly as
before; ``engine/halt.py`` is what lets anything *other* than this exact call — a later status
request, the recovery endpoint — durably reconstruct which stage failed, why, and what a human may
choose, purely from what this module has already persisted.

**Lease acquisition is the caller's responsibility, not this module's.** ``Sequencer.run`` requires
its caller to hand it a ``Deployment`` already in ``EXECUTING`` status (which itself requires a
held :class:`~groundwork_contracts.deployment.SubscriptionLease` per that type's own validator) —
matching ``api/deployments.py``'s own reasoning for not acquiring a lease at admission time. What
actually calls this with a queued deployment, acquires the lease, and releases it afterwards is the
queue-consumption loop, still to be added to ``groundwork_orchestrator.worker``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Protocol

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.audit import (
    ActorIdentity,
    ActorType,
    AuditOutcome,
    AuditRecord,
    AuthorityChain,
    DeploymentStageRecord,
    IdempotenceOutcome,
    RecoveryPath,
    StageError,
    StageStatus,
)
from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import DeploymentPlan
from groundwork_contracts.readiness import ValidationStatus
from groundwork_orchestrator.engine.checkpoints import record_checkpoint
from groundwork_orchestrator.engine.halt import (
    recovery_options_for_stage,
    requeue_after_recovery_choice,
)

if TYPE_CHECKING:
    from groundwork_contracts.audit import DeploymentReport
    from groundwork_orchestrator.engine.preview import WhatIfCaptureLike
    from groundwork_orchestrator.state.report_archive import ReportArchiveStoreLike
    from groundwork_orchestrator.state.repositories import ReportRepository
from groundwork_orchestrator.engine.cost_preflight import (
    COST_REAPPROVAL_PSEUDO_STAGE,
    CostPreflightError,
    CostRecheckResult,
)
from groundwork_orchestrator.engine.preflight import (
    PolicyPreflightError,
    PreflightContext,
)
from groundwork_orchestrator.engine.report_builder import build_report_content
from groundwork_orchestrator.engine.retry import (
    is_transient_error,
    load_stage_attempt_history,
    retry_not_yet_due,
    should_retry_after_failure,
)
from groundwork_orchestrator.state.audit_repository import AuditRepository
from groundwork_orchestrator.state.repositories import (
    CustomerTenantRepository,
    DeploymentRepository,
    StageRecordRepository,
)
from groundwork_shared.telemetry.metrics import record_stage_duration
from groundwork_shared.telemetry.scrubbing import scrub_text

logger = logging.getLogger(__name__)

AUDIT_RETENTION_DAYS = 365

_WHAT_IF_PSEUDO_STAGE = "what_if_preview"
"""Synthetic stage name for the what-if capture — not declared in any ``blueprint.yaml``, since it
is infrastructure only, but deliberately named so it shares the real halt/retry/recovery machinery
every real stage uses rather than a bespoke side channel."""

_WHAT_IF_RETRY_BUDGET = 3
"""Reasonable default for a read-only ARM operation that either submits or doesn't."""

_CONSENT_PSEUDO_STAGE = "consent_check"
"""Synthetic stage name for the per-stage consent re-check (T021e, FR-006 edge case: "consent
revocation during an in-flight deployment halts it rather than continuing"). Checked before every
real stage, not just ``infrastructure`` — unlike the policy/cost preflights, consent isn't a
property of one Azure call, it's the tenant's standing authority for this deployment to touch their
tenant at all, so a revocation must stop the very next stage regardless of which one it is.
``recovery_options=("retry",)``: once the customer re-consents (``consent_state`` back to
``GRANTED``), an operator-chosen retry resumes exactly like any other halt."""

_ADO_ORG_ACCESS_PSEUDO_STAGE = "ado_org_access_check"
"""Synthetic stage name for FR-038b's Azure DevOps organisation membership gate — checked only
immediately before ``devops_project``, unlike ``_CONSENT_PSEUDO_STAGE`` which runs before every
stage, because only ``devops_project`` actually calls the Azure DevOps REST API."""

_POLICY_PREFLIGHT_PSEUDO_STAGE = "policy_preflight"
"""Synthetic stage name for T075's execution-time policy re-check — same reasoning as
``_WHAT_IF_PSEUDO_STAGE``. Previously this halt was recorded under the real ``"infrastructure"``
stage name with no ``DeploymentStageRecord`` written at all, which meant ``engine/halt.py``'s
``reconstruct_halted_view`` (used by ``GET /deployments`` and ``api/recovery.py``) could not
reconstruct it after the fact — any call other than this exact ``Sequencer.run()`` invocation
would see the deployment as ``halted`` with no explanation. Fixed by giving this its own pseudo-
stage and always writing a record, the same discipline T074 already used."""


@dataclass(frozen=True, slots=True)
class StageExecutionContext:
    """What a :class:`Stage` needs to run once, for one deployment."""

    deployment: Deployment
    plan: DeploymentPlan
    credential: AsyncTokenCredential
    attempt: int
    devops_organization_url: str | None = None
    """The Azure DevOps organization this deployment's project is created in — per-tenant
    engagement data (``CustomerTenant.devops_organization_url``, resolved by the queue loop with
    the worker-wide setting as fallback), *not* stage-constructor configuration, because two
    tenants in one worker legitimately target two different organizations."""
    fabric_capacity_admin_upn: str | None = None
    """The customer-tenant user named as Fabric capacity administrator for this deployment
    (``CustomerTenant.fabric_capacity_admin_upn``, same resolution rule). Billable-to-customer
    identity data — a stage must fail on ``None`` rather than guess."""


@dataclass(frozen=True, slots=True)
class StageOutcome:
    """What a stage reports back — the sequencer, not the stage, owns timing and record ids.

    ``status`` must be ``SUCCEEDED``, ``FAILED``, or ``SKIPPED_CONVERGED`` — never ``STARTED``,
    which is a sequencer-owned transient state a stage's own return value cannot represent.
    """

    status: StageStatus
    resources_affected: tuple[str, ...] = ()
    idempotence_outcome: IdempotenceOutcome | None = None
    error: StageError | None = None
    resume_token: str = ""
    updated_deployment: Deployment | None = None

    def __post_init__(self) -> None:
        if self.status is StageStatus.STARTED:
            raise ValueError(
                "a Stage may not report STARTED as its own outcome; that status is owned by the "
                "sequencer, recorded before execute() is ever called"
            )
        if self.status is StageStatus.FAILED and self.error is None:
            raise ValueError("a FAILED outcome must carry an error (FR-031)")


class Stage(Protocol):
    """One blueprint stage's real implementation (``stages/*.py`` — T076-T083, not built yet)."""

    async def execute(self, context: StageExecutionContext) -> StageOutcome: ...


class PreflightCheck(Protocol):
    """An execution-time re-check run immediately before the infrastructure stage (T075).

    The only preflight this release needs is the policy re-check
    (:func:`groundwork_orchestrator.engine.preflight.recheck_no_denying_resource_type_policy`),
    but the sequencer takes this as an injectable protocol so tests can supply a fake one without
    needing a real Azure Policy SDK behind it."""

    async def __call__(self, context: PreflightContext) -> tuple[ValidationStatus, str]: ...


class CostPreflightCheck(Protocol):
    """An execution-time cost re-check run immediately before the infrastructure stage (T075a),
    injectable for the same reason ``PreflightCheck`` is: tests supply a fake without needing a
    real Retail Prices API call behind it. ``now`` is threaded through explicitly rather than the
    implementation calling a clock itself — the same determinism discipline ``_Clock`` enforces
    everywhere else in this module."""

    async def __call__(self, plan: DeploymentPlan, *, now: datetime) -> CostRecheckResult: ...


class NotifierLike(Protocol):
    """T096/FR-051: notify the customer once a deployment reaches a terminal, reportable state.

    Injectable for the same reason ``ReportArchiveStoreLike`` is — tests supply a fake, and a
    caller that does not want notification (a unit test) never has to fake an email provider to
    construct a working ``Sequencer``.

    ``tenant_id`` is passed so the concrete adapter (``worker.py``'s ``_TenantNotifier``) can
    look up ``CustomerTenant.notification_email`` — the resolved recipient address gathered and
    reconfirmed during the conversation (via ``ClarificationTracker.NOTIFICATION_EMAIL``).
    Notification is skipped when no email has been recorded, never silently dropped to a default.
    """

    async def notify_deployment_outcome(
        self, report: DeploymentReport, plan: DeploymentPlan, *, tenant_id: str, now: datetime
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class HaltedState:
    """Recorded on a halted deployment (FR-031). Which stage, why, and what a human may choose."""

    failing_stage: str
    error: StageError
    recovery_options: tuple[str, ...]


class SequencerError(Exception):
    """A run could not proceed at all — distinct from a stage reporting FAILED, which is a normal,
    fully-recorded outcome this module handles, not an exceptional one."""


@dataclass(frozen=True, slots=True)
class RunResult:
    deployment: Deployment
    halted: HaltedState | None = None


@dataclass(slots=True)
class _Clock:
    """Injectable ``now``, the same discipline as every other timestamp in this codebase —
    deterministic tests, no hidden dependency on the wall clock."""

    now_fn: Callable[[], datetime]

    def now(self) -> datetime:
        return self.now_fn()


class Sequencer:
    def __init__(
        self,
        blueprint: PlatformBlueprint,
        stages: dict[str, Stage],
        what_if_capture: WhatIfCaptureLike,
        *,
        deployment_repository: DeploymentRepository,
        stage_record_repository: StageRecordRepository,
        audit_repository: AuditRepository,
        actor_object_id: str,
        now_fn: Callable[[], datetime],
        tenant_repository: CustomerTenantRepository | None = None,
        policy_preflight: PreflightCheck | None = None,
        cost_preflight: CostPreflightCheck | None = None,
        report_archive_store: ReportArchiveStoreLike | None = None,
        report_repository: ReportRepository | None = None,
        notifier: NotifierLike | None = None,
    ) -> None:
        """
        Args:
            actor_object_id: The orchestrator's own workload identity object id (e.g.
                ``GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID``) — recorded as the actor on every audit
                record this sequencer writes. Never the plan's or approval's identifier; those
                belong on ``AuthorityChain``, a separate concern from *who executed* the stage.
            tenant_repository: Read-only lookup for the T021e consent re-check. ``None`` skips the
                check entirely (test doubles that construct a ``Deployment`` with no matching
                ``CustomerTenant`` on file) rather than raising — the same "absent means skip"
                discipline ``policy_preflight``/``cost_preflight`` already use.
        """
        declared = {s.name for s in blueprint.stages}
        missing = declared - set(stages)
        if missing:
            raise ValueError(
                f"no Stage implementation registered for {sorted(missing)}; every blueprint "
                f"stage must be executable (FR-028)"
            )
        self._blueprint = blueprint
        self._stages = stages
        self._deployment_repository = deployment_repository
        self._stage_record_repository = stage_record_repository
        self._actor_object_id = actor_object_id
        self._audit_repository = audit_repository
        self._what_if_capture = what_if_capture
        self._tenant_repository = tenant_repository
        self._policy_preflight = policy_preflight
        self._cost_preflight = cost_preflight
        self._report_archive_store = report_archive_store
        self._report_repository = report_repository
        self._notifier = notifier
        self._clock = _Clock(now_fn)

    async def run(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        *,
        credential: AsyncTokenCredential,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult:
        if deployment.status is not DeploymentStatus.EXECUTING:
            raise SequencerError(
                f"deployment {deployment.deployment_id} is {deployment.status.value!r}, not "
                f"'executing'; the caller must transition it and hold its subscription lease "
                f"before calling Sequencer.run (see module docstring)"
            )

        order = self._blueprint.execution_order()
        resume_after = deployment.checkpoint.stage_name if deployment.checkpoint else None
        skipping = resume_after is not None
        current = deployment

        # T074/FR-024: capture one what-if preview before any real stage attempts a write.
        # Skipped when resuming (a checkpoint exists — the preview was already captured) or when
        # the artefact already exists (idempotence — a prior partial run captured it before
        # halting later).
        if not skipping and deployment.what_if_artefact_uri is None:
            current, halted = await self._capture_what_if(current, plan, credential)
            if halted is not None:
                return RunResult(deployment=current, halted=halted)
            # _capture_what_if may have requeued the deployment (transient failure within budget)
            # — if so, return immediately so the queue loop can retry later, exactly like the
            # per-stage transient path does.
            if current.status is not DeploymentStatus.EXECUTING:
                return RunResult(deployment=current)

        for stage_name in order:
            if skipping:
                if stage_name == resume_after:
                    skipping = False
                continue

            stage_meta = next(s for s in self._blueprint.stages if s.name == stage_name)
            history = await load_stage_attempt_history(
                self._stage_record_repository, current.tenant_id, current.deployment_id, stage_name
            )

            not_yet_due = retry_not_yet_due(
                minimum_retry_interval_seconds=stage_meta.minimum_retry_interval_seconds,
                last_attempt_ended_at=history.last_ended_at,
                now=self._clock.now(),
            )
            if not_yet_due is not None:
                # T072/FR-030: a prior attempt failed transiently and the blueprint's own
                # minimum-retry-interval has not yet elapsed (e.g. fabric's 900s after a managed
                # private endpoint deletion). Do not call stage.execute() at all — no wasted Azure
                # call, no wasted audit/stage record — just leave the deployment queued for a later
                # poll cycle to try again. See retry.py's own module docstring for why this is a
                # requeue, not an in-process sleep.
                current = await self._mark_requeued(current)
                return RunResult(deployment=current)

            gate_result = await self._check_preflight_gates(
                current,
                plan,
                stage_name,
                credential,
                devops_organization_url=devops_organization_url,
                fabric_capacity_admin_upn=fabric_capacity_admin_upn,
            )
            if gate_result is not None:
                return gate_result

            current, outcome = await self._run_one_stage(
                current,
                plan,
                credential,
                stage_name,
                attempt=history.attempt_count + 1,
                devops_organization_url=devops_organization_url,
                fabric_capacity_admin_upn=fabric_capacity_admin_upn,
            )

            if outcome.status is StageStatus.FAILED:
                is_transient = outcome.error.is_transient if outcome.error else False
                if should_retry_after_failure(
                    is_transient=is_transient,
                    retry_budget=stage_meta.retry_budget,
                    attempts_so_far=history.attempt_count + 1,
                ):
                    current = await self._mark_requeued(current)
                    return RunResult(deployment=current)

                error = outcome.error
                if error is None:
                    raise SequencerError("FAILED stage outcome missing error")
                halted = HaltedState(
                    failing_stage=stage_name,
                    error=error,
                    recovery_options=recovery_options_for_stage(self._blueprint, stage_name),
                )
                current = await self._mark_halted(current, halted, plan)
                return RunResult(deployment=current, halted=halted)

            try:
                current = await record_checkpoint(
                    self._deployment_repository,
                    current,
                    completed_stage=stage_name,
                    resume_token=outcome.resume_token,
                    now=self._clock.now(),
                )
            except Exception as exc:
                # Same reasoning _run_one_stage already applies to a raising stage.execute(): a
                # completed, SUCCEEDED stage whose own checkpoint write then fails must not leave
                # the deployment silently orphaned at status=executing forever — the stage's own
                # DeploymentStageRecord (already written by _run_one_stage above) says it succeeded,
                # so this halts as this specific stage's own failure, not a generic crash. Found
                # live 2026-08-25 (AGENT_HANDOFF.md §4 bug #7): a Checkpoint validation error here
                # had no equivalent guard, and queue_loop.py's own outermost handler logs and
                # swallows it without ever transitioning the deployment out of EXECUTING.
                halted = HaltedState(
                    failing_stage=stage_name,
                    error=StageError(
                        code=type(exc).__name__,
                        message=scrub_text(str(exc)) or "checkpoint write raised with no message",
                        is_transient=is_transient_error(exc),
                    ),
                    recovery_options=recovery_options_for_stage(self._blueprint, stage_name),
                )
                current = await self._mark_halted(current, halted, plan)
                return RunResult(deployment=current, halted=halted)

        current = await self._mark_succeeded(current, plan)
        return RunResult(deployment=current)

    async def _check_preflight_gates(
        self,
        current: Deployment,
        plan: DeploymentPlan,
        stage_name: str,
        credential: AsyncTokenCredential,
        *,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult | None:
        """Run every preflight gate for ``stage_name``; returns ``None`` if all pass.

        Each gate follows the same shape: check a condition that may have changed since plan
        time, and if it now blocks execution, halt with a gate-specific pseudo-stage and error.
        Extracted from ``run()`` so the stage loop reads as "check gates, run stage" rather than
        interleaving ~120 lines of gate logic with the iteration itself. Adding a new gate means
        adding one more ``if`` block here, not another inline block in ``run()``.
        """

        # T021e/FR-006: consent re-check before every stage — a customer revoking consent
        # mid-deployment must halt the very next stage attempted.
        tenant = (
            await self._tenant_repository.read(current.tenant_id, current.tenant_id)
            if self._tenant_repository is not None
            else None
        )
        if tenant is not None and not tenant.consent_state.permits_tenant_operations:
            halted = await self._halt_preflight(
                current,
                stage_name=_CONSENT_PSEUDO_STAGE,
                error=StageError(
                    code="ConsentNoLongerGranted",
                    message=(
                        f"tenant consent_state is {tenant.consent_state.value!r}, not "
                        f"'granted'; halting before the {stage_name!r} stage"
                    ),
                    is_transient=False,
                ),
                recovery_options=("retry",),
            )
            current = await self._mark_halted(current, halted, plan)
            return RunResult(deployment=current, halted=halted)

        # FR-038b: ADO organisation membership is not covered by consent_state.
        if (
            stage_name == "devops_project"
            and tenant is not None
            and not tenant.ado_org_access_state.permits_devops_project_execution
        ):
            halted = await self._halt_preflight(
                current,
                stage_name=_ADO_ORG_ACCESS_PSEUDO_STAGE,
                error=StageError(
                    code="AdoOrgAccessNotGranted",
                    message=(
                        f"tenant ado_org_access_state is "
                        f"{tenant.ado_org_access_state.value!r}, not 'granted'; halting "
                        f"before the {stage_name!r} stage"
                    ),
                    is_transient=False,
                ),
                recovery_options=("retry",),
            )
            current = await self._mark_halted(current, halted, plan)
            return RunResult(deployment=current, halted=halted)

        # T075: policy preflight before infrastructure.
        if stage_name == "infrastructure" and self._policy_preflight is not None:
            ctx = PreflightContext(
                subscription_id=plan.subscription_id,
                credential=credential,
                region=plan.region.value,
            )
            try:
                status, detail = await self._policy_preflight(ctx)
            except PolicyPreflightError as exc:
                status = ValidationStatus.UNREACHABLE
                detail = str(exc)
            if status is not ValidationStatus.PASSED:
                halted = await self._halt_preflight(
                    current,
                    stage_name=_POLICY_PREFLIGHT_PSEUDO_STAGE,
                    error=StageError(
                        code="PolicyPreflightFailed",
                        message=scrub_text(detail),
                        is_transient=False,
                    ),
                    recovery_options=("retry", "forward_fix"),
                )
                current = await self._mark_halted(current, halted, plan)
                return RunResult(deployment=current, halted=halted)

        # T075a/FR-019: cost re-check before infrastructure.
        if stage_name == "infrastructure" and self._cost_preflight is not None:
            try:
                cost_result = await self._cost_preflight(plan, now=self._clock.now())
            except CostPreflightError as exc:
                halted = await self._halt_preflight(
                    current,
                    stage_name=COST_REAPPROVAL_PSEUDO_STAGE,
                    error=StageError(
                        code="CostPreflightUnreachable",
                        message=scrub_text(str(exc)),
                        is_transient=False,
                    ),
                    recovery_options=("retry",),
                )
                current = await self._mark_halted(current, halted, plan)
                return RunResult(deployment=current, halted=halted)
            if not cost_result.within_band:
                error_code = (
                    "CostReapprovalEscalationRequired"
                    if cost_result.crosses_second_approver_threshold
                    else "CostReapprovalRequired"
                )
                halted = await self._halt_preflight(
                    current,
                    stage_name=COST_REAPPROVAL_PSEUDO_STAGE,
                    error=StageError(
                        code=error_code,
                        message=scrub_text(cost_result.detail),
                        is_transient=False,
                    ),
                    recovery_options=("retry",),
                )
                current = await self._mark_halted(current, halted, plan)
                return RunResult(deployment=current, halted=halted)

        return None

    async def _run_one_stage(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        credential: AsyncTokenCredential,
        stage_name: str,
        *,
        attempt: int,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> tuple[Deployment, StageOutcome]:
        started_at = self._clock.now()
        marked_current = await self._deployment_repository.replace(
            deployment.tenant_id, deployment.model_copy(update={"current_stage": stage_name})
        )

        stage = self._stages[stage_name]
        context = StageExecutionContext(
            deployment=marked_current,
            plan=plan,
            credential=credential,
            attempt=attempt,
            devops_organization_url=devops_organization_url,
            fabric_capacity_admin_upn=fabric_capacity_admin_upn,
        )

        audit_record = AuditRecord(
            audit_id=str(uuid.uuid4()),
            tenant_id=deployment.tenant_id,
            correlation_id=deployment.correlation_id,
            deployment_id=deployment.deployment_id,
            sequence=self._sequence_for(stage_name),
            occurred_at=started_at,
            actor=ActorIdentity(
                object_id=self._actor_object_id,
                display_name="groundwork-orchestrator",
                actor_type=ActorType.WORKLOAD,
            ),
            action=f"execute_stage:{stage_name}",
            is_mutating=True,
            authorised_by=AuthorityChain(
                plan_hash=deployment.authority.plan_hash,
                approval_id=deployment.authority.approval_id,
            ),
            outcome=AuditOutcome.AUTHORISED,
            retention_expires_at=self._retention_deadline(started_at),
        )

        stage_meta = next(s for s in self._blueprint.stages if s.name == stage_name)
        timeout_seconds = stage_meta.timeout_seconds
        try:
            outcome = await self._audit_repository.record_before(
                deployment.tenant_id,
                audit_record,
                lambda: asyncio.wait_for(stage.execute(context), timeout=timeout_seconds),
            )
        except TimeoutError:
            # A hang is a different failure mode from a raise — nothing above catches a call that
            # simply never returns. Classified transient: the same retry budget and requeue path
            # a raised transient exception already takes, since a hang is at least as likely to be
            # a slow dependency as a genuine defect (module docstring point 5).
            outcome = StageOutcome(
                status=StageStatus.FAILED,
                error=StageError(
                    code="StageTimeout",
                    message=(
                        f"stage {stage_name!r} exceeded its {stage_meta.timeout_seconds}s bound"
                    ),
                    is_transient=True,
                ),
            )
        except Exception as exc:
            # A stage that raises (a network error, an unexpected API response, an auth failure)
            # must halt-and-preserve exactly like one that returns FAILED — not crash the whole
            # run uncaught, which would leave the deployment stuck EXECUTING with no record of
            # why. Same reasoning as ReadinessEngine turning a raised check into UNREACHABLE.
            # is_transient is real classification now (T072, retry.py's own is_transient_error) —
            # a network-level failure or a retryable HTTP/Azure response is worth another attempt;
            # everything else, including every stage's own named domain exception, defaults to
            # permanent, the same conservative default this module used before T072 existed.
            outcome = StageOutcome(
                status=StageStatus.FAILED,
                error=StageError(
                    code=type(exc).__name__,
                    message=scrub_text(str(exc)) or "stage raised with no message",
                    is_transient=is_transient_error(exc),
                ),
            )

        ended_at = self._clock.now()
        record_stage_duration(
            stage_name=stage_name,
            outcome=outcome.status.value,
            duration_seconds=(ended_at - started_at).total_seconds(),
        )
        await self._stage_record_repository.create(
            deployment.tenant_id,
            DeploymentStageRecord(
                record_id=str(uuid.uuid4()),
                deployment_id=deployment.deployment_id,
                tenant_id=deployment.tenant_id,
                stage_name=stage_name,
                attempt=context.attempt,
                status=outcome.status,
                started_at=started_at,
                ended_at=ended_at,
                error=outcome.error,
                resources_affected=outcome.resources_affected,
                idempotence_outcome=outcome.idempotence_outcome,
                recovery_path=self._recovery_path_enum(stage_name) if outcome.error else None,
            ),
        )

        return outcome.updated_deployment or marked_current, outcome

    async def _capture_what_if(
        self, deployment: Deployment, plan: DeploymentPlan, credential: AsyncTokenCredential
    ) -> tuple[Deployment, HaltedState | None]:
        """T074/FR-024: what-if capture as a synthetic pseudo-stage.

        Shares the exact same halt/retry/recovery machinery every real blueprint stage uses —
        loads attempt history, writes a ``DeploymentStageRecord``, classifies transient errors,
        requeues or halts with recovery options — rather than a bespoke side channel with different
        semantics. On success, sets ``what_if_artefact_uri`` on the deployment so a later resume
        skips this without needing a checkpoint.

        Returns ``(deployment, None)`` on success (proceed to real stages), or
        ``(deployment, HaltedState)`` on a permanent failure (caller returns immediately).
        """
        stage_name = _WHAT_IF_PSEUDO_STAGE
        history = await load_stage_attempt_history(
            self._stage_record_repository,
            deployment.tenant_id,
            deployment.deployment_id,
            stage_name,
        )
        attempt = history.attempt_count + 1

        started_at = self._clock.now()

        try:
            artefact_uri = await self._what_if_capture.capture(
                deployment=deployment, plan=plan, credential=credential
            )
            outcome = StageOutcome(status=StageStatus.SUCCEEDED, resume_token=artefact_uri)
        except Exception as exc:
            outcome = StageOutcome(
                status=StageStatus.FAILED,
                error=StageError(
                    code=type(exc).__name__,
                    message=scrub_text(str(exc)) or "what-if capture raised with no message",
                    is_transient=is_transient_error(exc),
                ),
            )

        ended_at = self._clock.now()
        await self._stage_record_repository.create(
            deployment.tenant_id,
            DeploymentStageRecord(
                record_id=str(uuid.uuid4()),
                deployment_id=deployment.deployment_id,
                tenant_id=deployment.tenant_id,
                stage_name=stage_name,
                attempt=attempt,
                status=outcome.status,
                started_at=started_at,
                ended_at=ended_at,
                error=outcome.error,
                resources_affected=(),
                idempotence_outcome=None,
                recovery_path=RecoveryPath.FORWARD_FIX if outcome.error else None,
            ),
        )

        if outcome.status is StageStatus.SUCCEEDED:
            updated = deployment.model_copy(update={"what_if_artefact_uri": outcome.resume_token})
            persisted = await self._deployment_repository.replace(deployment.tenant_id, updated)
            logger.debug("what-if captured artefact stored at %s", outcome.resume_token)
            return persisted, None

        # Capture failed — apply the same retry/halt logic as any real stage.
        is_transient = outcome.error.is_transient if outcome.error else False
        if should_retry_after_failure(
            is_transient=is_transient,
            retry_budget=_WHAT_IF_RETRY_BUDGET,
            attempts_so_far=attempt,
        ):
            requeued = await self._mark_requeued(deployment)
            return requeued, None

        error = outcome.error
        if error is None:
            raise SequencerError("FAILED what-if outcome missing error")
        halted = HaltedState(
            failing_stage=stage_name,
            error=error,
            recovery_options=recovery_options_for_stage(self._blueprint, stage_name),
        )
        halted_deployment = await self._mark_halted(deployment, halted, plan)
        return halted_deployment, halted

    def _sequence_for(self, stage_name: str) -> int:
        return self._blueprint.execution_order().index(stage_name)

    @staticmethod
    def _retention_deadline(occurred_at: datetime) -> datetime:
        return occurred_at + timedelta(days=AUDIT_RETENTION_DAYS)

    def _recovery_path_enum(self, stage_name: str) -> RecoveryPath:
        """Derive the ``DeploymentStageRecord.recovery_path`` enum from the blueprint stage's own
        free-text ``recovery_path`` prose.

        Disclosed heuristic, not a structured field: ``BlueprintStage.recovery_path`` is
        human-readable prose (``blueprint.yaml``), not a ``RecoveryPath`` enum value. Checked
        against every stage in ``standard-production-fabric/blueprint.yaml`` — the only blueprint
        this release has — the signal is reliable: a stage whose prose mentions "rollback" is
        exactly the set that names rollback as an available (approval-gated) recovery, regardless
        of whether its prose *leads* with "Halt and preserve" (infrastructure, fabric) or not; of
        the rest, exactly one leads with "Retry" (validation_tests) and the remainder with
        "Forward-fix". A second blueprint whose prose doesn't follow this convention would need a
        real structured field on `BlueprintStage` instead of this heuristic — not attempted here
        since there is only one blueprint to check it against.
        """
        stage = next(s for s in self._blueprint.stages if s.name == stage_name)
        text = stage.recovery_path.lower()
        if "rollback" in text:
            return RecoveryPath.ROLLBACK
        if text.startswith("retry"):
            return RecoveryPath.RETRY
        return RecoveryPath.FORWARD_FIX

    async def _halt_preflight(
        self,
        deployment: Deployment,
        *,
        stage_name: str,
        error: StageError,
        recovery_options: tuple[str, ...],
    ) -> HaltedState:
        """Build a :class:`HaltedState` for a preflight check (T075 policy, T075a cost) that runs
        before any real stage attempts a write, **and durably record it** as a
        :class:`DeploymentStageRecord` under a synthetic pseudo-stage name.

        The record is not optional. A preflight halt that only returns a transient
        ``RunResult.halted`` — without persisting anything — is invisible to any caller other than
        this exact ``Sequencer.run()`` invocation: ``engine/halt.py``'s
        ``reconstruct_halted_view`` (what ``GET /deployments/{id}`` and ``api/recovery.py`` both
        use) reconstructs a halt reason from the most-recently-ended stage record, and a
        deployment halted with no record at all reconstructs to "nothing to report" — the recovery
        endpoint would then 500 on a deployment that is very much halted. T074's what-if capture
        got this right from the start (see ``_capture_what_if``); T075's policy preflight did not
        when first built, which is the bug this shared helper exists to make impossible to repeat.
        """
        now = self._clock.now()
        await self._stage_record_repository.create(
            deployment.tenant_id,
            DeploymentStageRecord(
                record_id=str(uuid.uuid4()),
                deployment_id=deployment.deployment_id,
                tenant_id=deployment.tenant_id,
                stage_name=stage_name,
                attempt=1,
                status=StageStatus.FAILED,
                started_at=now,
                ended_at=now,
                error=error,
                recovery_path=RecoveryPath.FORWARD_FIX,
            ),
        )
        return HaltedState(failing_stage=stage_name, error=error, recovery_options=recovery_options)

    async def _mark_halted(
        self, deployment: Deployment, halted: HaltedState, plan: DeploymentPlan
    ) -> Deployment:
        # T086a/FR-054: the signal alert-gw-deployment-failures (observability.bicep) queries
        # (`AppTraces | where Properties.event == "deployment_failed"`). Logged here, not deeper in
        # the call stack, because this is the one place every halt path — a real stage's FAILED
        # outcome, a raising stage, a failing preflight — converges before returning to the caller.
        logger.warning(
            "deployment halted",
            extra={
                "component": "orchestrator",
                "event": "deployment_failed",
                "deployment_id": deployment.deployment_id,
                "tenant_id": deployment.tenant_id,
                "failing_stage": halted.failing_stage,
                "error_code": halted.error.code,
            },
        )
        updated = deployment.model_copy(
            update={
                "status": DeploymentStatus.HALTED,
                "current_stage": None,
                "completed_at": self._clock.now(),
            }
        )
        persisted = await self._deployment_repository.replace(deployment.tenant_id, updated)
        await self._generate_report(persisted, plan)
        return persisted

    async def _mark_succeeded(self, deployment: Deployment, plan: DeploymentPlan) -> Deployment:
        updated = deployment.model_copy(
            update={
                "status": DeploymentStatus.SUCCEEDED,
                "current_stage": None,
                "completed_at": self._clock.now(),
            }
        )
        persisted = await self._deployment_repository.replace(deployment.tenant_id, updated)
        await self._generate_report(persisted, plan)
        return persisted

    async def _generate_report(self, deployment: Deployment, plan: DeploymentPlan) -> None:
        """T092/T093/FR-052: produce and archive this deployment's report, the moment it reaches a
        terminal, reportable state (``SUCCEEDED`` or ``HALTED`` — see ``report_builder.py``'s own
        ``_REPORTABLE_OUTCOMES``; ``ROLLED_BACK`` is currently unreachable, since rollback execution
        itself is a disclosed, out-of-scope gap — ``api/recovery.py``'s own module docstring).

        A no-op when ``report_archive_store``/``report_repository`` were not injected — matching
        ``policy_preflight``/``cost_preflight``'s own optional-by-default pattern, so a caller that
        genuinely does not want report generation (a unit test, most obviously) never has to fake
        blob storage to construct a working ``Sequencer``.

        Never raises past this point: a failure to generate a report must not turn a successful or
        cleanly-halted deployment into an unhandled crash. Logged and swallowed, matching the same
        "one bad thing must not stop the rest" discipline ``queue_loop.py``'s own outer try/except
        already applies at the level above this call.

        T096/FR-051: notification is attempted after a successful archive, only if ``notifier`` was
        injected. The concrete adapter (``worker.py``'s ``_TenantNotifier``) resolves the recipient
        from ``CustomerTenant.notification_email`` — the address gathered and reconfirmed during
        the conversation. A notification failure is logged under its own event, separately from a
        report generation failure, and never turns an already-persisted report into a retried write.
        """
        if self._report_archive_store is None or self._report_repository is None:
            return
        try:
            content = await build_report_content(
                deployment,
                plan,
                stage_record_repository=self._stage_record_repository,
                blueprint=self._blueprint,
            )
            report = await self._report_archive_store.archive(content, now=self._clock.now())
            existing = await self._report_repository.read(
                deployment.tenant_id, deployment.deployment_id
            )
            if existing is None:
                await self._report_repository.create(deployment.tenant_id, report)
            else:
                await self._report_repository.replace(deployment.tenant_id, report)
        except Exception:
            logger.error(
                "report generation failed",
                exc_info=True,
                extra={
                    "component": "orchestrator",
                    "operation": "report_generation",
                    "tenant_id": deployment.tenant_id,
                    "deployment_id": deployment.deployment_id,
                },
            )
            return

        if self._notifier is not None:
            try:
                await self._notifier.notify_deployment_outcome(
                    report, plan, tenant_id=deployment.tenant_id, now=self._clock.now()
                )
            except Exception:
                logger.error(
                    "notification dispatch failed",
                    exc_info=True,
                    extra={
                        "component": "orchestrator",
                        "operation": "notification_dispatch",
                        "tenant_id": deployment.tenant_id,
                        "deployment_id": deployment.deployment_id,
                    },
                )

    async def _mark_requeued(self, deployment: Deployment) -> Deployment:
        """T072/FR-030: a retryable stage failure, or one whose minimum retry interval has not yet
        elapsed. Returns the deployment to ``queued`` rather than ``halted`` — the checkpoint
        already correctly points at the last *completed* stage (this one never reached it), so the
        next ``Sequencer.run`` call the queue-consumption loop makes for this deployment resumes
        and retries exactly the stage that needs it, nothing more.

        Must clear ``started_at`` and ``lease`` along with ``status``/``current_stage`` —
        ``Deployment``'s own ``_terminal_and_active_states_match_timestamps`` validator requires a
        ``queued`` deployment to have no ``started_at``. ``model_copy`` does not re-run validators
        (this codebase's established, deliberate discipline — see ``_mark_halted``/
        ``_mark_succeeded`` above, which follow the same pattern), so an incomplete update here
        would not fail loudly now; it would fail the *next* time this document is read back from
        Cosmos and validated, which is a far worse place to discover it. The lease itself is
        already released by the queue-consumption loop's own caller (``queue_loop.py``'s
        ``finally``) by the time anything re-reads this document; clearing the field here just
        keeps the document honest about that. Field-clearing itself lives in ``engine/halt.py``'s
        ``requeue_after_recovery_choice`` — shared with ``api/recovery.py``'s human-chosen
        retry/forward-fix path, so there is one rule for what a safe requeue clears, not two."""
        updated = requeue_after_recovery_choice(deployment)
        return await self._deployment_repository.replace(deployment.tenant_id, updated)
