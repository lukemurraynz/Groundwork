"""Audit records, stage records, and deployment reports.

The audit-by-construction rule: the record is written *before* the action it authorises, and
every mutating action traces to an approved plan and an authorising human.

Two type-level decisions carry that:

- :class:`AuditRecord` requires an :class:`AuthorityChain` whenever the action is mutating. There
  is no way to construct a mutating audit entry without naming the plan and approval that
  authorised it, so "an action with no authority chain" is unrepresentable rather than merely
  discouraged (FR-047).
- :class:`ActorType` includes ``AGENT`` so that an agent-attributed mutating action can be
  *rejected by name*. Omitting it would make the same attempt fail as a vague schema error.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

_GUID = (
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

# FR-052a: audit records and reports retained 12 months, matching conversation retention.
AUDIT_RETENTION = timedelta(days=365)


class ActorType(StrEnum):
    HUMAN = "human"
    WORKLOAD = "workload"
    SYSTEM = "system"
    AGENT = "agent"


class AuditOutcome(StrEnum):
    AUTHORISED = "authorised"
    DENIED = "denied"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StageStatus(StrEnum):
    """Outcome of one stage attempt.

    ``SKIPPED_CONVERGED`` means the stage ran, found converged state, and correctly did nothing —
    which is the FR-029 idempotence contract being satisfied. It is distinct from a stage that
    never executed, which is ``FAILED`` per FR-034. Collapsing the two would let an unexecuted
    stage read as a successful no-op.
    """

    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED_CONVERGED = "skipped_converged"


class IdempotenceOutcome(StrEnum):
    APPLIED = "applied"
    NO_OP = "no_op"


class RecoveryPath(StrEnum):
    RETRY = "retry"
    FORWARD_FIX = "forward_fix"
    ROLLBACK = "rollback"


class ReportOutcome(StrEnum):
    """Terminal outcome of a deployment.

    There is no ``PARTIAL_SUCCESS``. FR-034 requires a partial deployment to be reported as a
    failure, so a partially-complete deployment is ``HALTED``, never ``SUCCEEDED``.
    """

    SUCCEEDED = "succeeded"
    HALTED = "halted"
    ROLLED_BACK = "rolled_back"


class ActorIdentity(BaseModel):
    model_config = StrictModel

    object_id: Annotated[str, Field(pattern=_GUID)]
    display_name: Annotated[str, Field(min_length=1)]
    actor_type: ActorType


class AuthorityChain(BaseModel):
    """What authorised a mutating action (FR-047).

    Both fields are required. A chain missing either cannot answer the auditor's question — who
    approved this, and what exactly did they approve. No separate ``plan_id`` GUID: this system's
    actual plan identity is ``plan_hash`` alone — see
    ``groundwork_contracts.approval.Approval``'s docstring for why a plan has no independent GUID
    for an authority chain to reference.
    """

    model_config = StrictModel

    plan_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    approval_id: Annotated[str, Field(pattern=_GUID)]


class AuditRecord(BaseModel):
    """An immutable, append-only attribution of one action.

    ``sequence`` is monotonic per deployment so that a gap is detectable. An audit trail that
    cannot reveal a missing entry provides weaker assurance than it appears to.
    """

    model_config = StrictModel

    audit_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    correlation_id: Annotated[str, Field(pattern=_GUID)]
    deployment_id: Annotated[str, Field(pattern=_GUID)] | None = None
    sequence: Annotated[int, Field(ge=0)]
    occurred_at: datetime
    actor: ActorIdentity
    action: Annotated[str, Field(min_length=1)]
    is_mutating: bool
    authorised_by: AuthorityChain | None = None
    outcome: AuditOutcome
    details: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    retention_expires_at: datetime

    @model_validator(mode="after")
    def _mutating_actions_have_authority(self) -> Self:
        # FR-047: an action that cannot be traced to an approved plan is a defect, not an
        # untracked success. Enforced here so it cannot be written at all.
        if self.is_mutating and self.authorised_by is None:
            raise ValueError(
                f"mutating action {self.action!r} has no authority chain; FR-047 requires every "
                f"mutating action to trace to an approved plan and an authorising identity"
            )
        return self

    @model_validator(mode="after")
    def _agents_never_mutate(self) -> Self:
        # The deterministic-execution boundary: a model never performs a mutating action. If one is
        # ever attributed to an agent, that is a boundary breach and must fail loudly rather than
        # be recorded.
        if self.is_mutating and self.actor.actor_type is ActorType.AGENT:
            raise ValueError(
                f"mutating action {self.action!r} attributed to an agent actor; the "
                f"deterministic-execution boundary forbids an agent performing a mutating action"
            )
        return self

    @model_validator(mode="after")
    def _retention_within_policy(self) -> Self:
        if self.retention_expires_at > self.occurred_at + AUDIT_RETENTION:
            raise ValueError(
                f"retention_expires_at exceeds the {AUDIT_RETENTION.days}-day period set by FR-052a"
            )
        return self


class StageError(BaseModel):
    """A stage failure, already scrubbed of secrets.

    Provider errors routinely contain URLs with tokens. Scrubbing is the caller's responsibility
    before construction; ``raw_payload`` is deliberately absent so there is nowhere to put an
    unscrubbed body (FR-049).
    """

    model_config = StrictModel

    code: Annotated[str, Field(min_length=1)]
    message: Annotated[str, Field(min_length=1)]
    is_transient: bool


class DeploymentStageRecord(BaseModel):
    """One stage attempt. Append-only; prior attempts are retained (FR-030)."""

    model_config = StrictModel

    record_id: Annotated[str, Field(pattern=_GUID)]
    deployment_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    stage_name: Annotated[str, Field(min_length=1)]
    attempt: Annotated[int, Field(ge=1)]
    status: StageStatus
    started_at: datetime
    ended_at: datetime | None = None
    error: StageError | None = None
    resources_affected: tuple[str, ...] = ()
    idempotence_outcome: IdempotenceOutcome | None = None
    recovery_path: RecoveryPath | None = None

    @model_validator(mode="after")
    def _terminal_states_are_complete(self) -> Self:
        terminal = {
            StageStatus.SUCCEEDED,
            StageStatus.FAILED,
            StageStatus.SKIPPED_CONVERGED,
        }
        if self.status in terminal and self.ended_at is None:
            raise ValueError(
                f"stage {self.stage_name!r} is {self.status.value} but has no ended_at"
            )
        if self.status is StageStatus.STARTED and self.ended_at is not None:
            raise ValueError(f"stage {self.stage_name!r} is 'started' but has an ended_at")
        return self

    @model_validator(mode="after")
    def _failures_declare_recovery(self) -> Self:
        # FR-031: a stage with no declared recovery path must not be marked complete.
        if self.status is StageStatus.FAILED:
            if self.error is None:
                raise ValueError(
                    f"stage {self.stage_name!r} failed but records no error; the failure must be "
                    f"attributable (FR-031)"
                )
            if self.recovery_path is None:
                raise ValueError(
                    f"stage {self.stage_name!r} failed but declares no recovery path; FR-031 "
                    f"requires retry, forward-fix, or rollback to be offered"
                )
        return self

    @model_validator(mode="after")
    def _converged_is_a_no_op(self) -> Self:
        if (
            self.status is StageStatus.SKIPPED_CONVERGED
            and self.idempotence_outcome is not IdempotenceOutcome.NO_OP
        ):
            raise ValueError(
                f"stage {self.stage_name!r} reports converged state but its idempotence outcome "
                f"is not 'no_op'; FR-029 requires re-execution against converged state to be a "
                f"no-op"
            )
        return self


class StageSummary(BaseModel):
    """Per-stage outcome for the report.

    ``never_ran`` is a first-class state. SC-015 requires a failed deployment's report to state
    which stages succeeded, failed, **and never ran** — omitting the third would let a reader
    assume unlisted stages were fine.
    """

    model_config = StrictModel

    stage_name: Annotated[str, Field(min_length=1)]
    status: StageStatus | None = None
    never_ran: bool = False
    attempts: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _never_ran_has_no_status(self) -> Self:
        if self.never_ran and self.status is not None:
            raise ValueError(f"stage {self.stage_name!r} is marked never_ran but carries a status")
        if not self.never_ran and self.status is None:
            raise ValueError(f"stage {self.stage_name!r} has no status and is not marked never_ran")
        return self


class DeploymentReport(BaseModel):
    """The durable customer-facing and auditor-facing record of a finished deployment."""

    model_config = StrictModel

    report_id: Annotated[str, Field(pattern=_GUID)]
    deployment_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    correlation_id: Annotated[str, Field(pattern=_GUID)]
    authority: AuthorityChain
    outcome: ReportOutcome
    stage_summary: Annotated[tuple[StageSummary, ...], Field(min_length=1)]
    resources_created: tuple[str, ...] = ()
    iac_artefact_versions: dict[str, str] = Field(default_factory=dict)
    final_monthly_cost_aud: Annotated[float, Field(ge=0)]
    blob_uri: Annotated[str, Field(min_length=1)]
    content_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    accessibility_conformance: Annotated[str, Field(pattern=r"^WCAG-2\.2-AA$")] = "WCAG-2.2-AA"
    generated_at: datetime
    retention_expires_at: datetime

    @model_validator(mode="after")
    def _success_means_every_stage_succeeded(self) -> Self:
        # FR-034 and SC-015: a report must never present a partial deployment as a success.
        if self.outcome is ReportOutcome.SUCCEEDED:
            incomplete = [
                s.stage_name
                for s in self.stage_summary
                if s.never_ran
                or s.status not in (StageStatus.SUCCEEDED, StageStatus.SKIPPED_CONVERGED)
            ]
            if incomplete:
                raise ValueError(
                    f"report claims success but stage(s) {sorted(incomplete)} did not complete; "
                    f"FR-034 forbids reporting a partial deployment as successful"
                )
        return self

    @model_validator(mode="after")
    def _retention_within_policy(self) -> Self:
        if self.retention_expires_at > self.generated_at + AUDIT_RETENTION:
            raise ValueError(
                f"retention_expires_at exceeds the {AUDIT_RETENTION.days}-day period set by FR-052a"
            )
        return self

    def is_expired_at(self, moment: datetime) -> bool:
        """Whether the report has passed its retention period.

        FR-052b requires an expired report to be distinguishable from one that never existed, so
        the API can avoid implying that a deployment did not occur.
        """
        return moment > self.retention_expires_at
