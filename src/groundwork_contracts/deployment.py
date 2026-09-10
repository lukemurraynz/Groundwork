"""The Deployment record — durable execution state (FR-027, FR-030, FR-035, FR-045b).

A :class:`Deployment` is issued once at approval and then only ever appended to by the orchestrator
as execution progresses. It is deliberately separate from :class:`~groundwork_contracts.plan.
DeploymentPlan`: the plan is what was approved, the deployment is the durable record of *attempting*
that plan, and the two must be able to diverge (a plan can be approved once and an execution attempt
can be retried, resumed on a different replica, or halted) without either overwriting the other.

Two invariants drive the shape here, both from the original design notes:

- **Resume invariant (FR-035)**: a replica taking over an execution must be able to resume from
  ``checkpoint`` alone. Nothing about resumability may depend on state held only in the process that
  was previously executing — that state does not survive a pod eviction.
- **Serialisation invariant (FR-045b)**: at most one deployment per ``subscription_id`` may be
  ``EXECUTING`` at a time. That cannot be expressed as a single-instance validator — it requires
  knowing about *other* documents — so it is enforced by the lease document in the
  ``subscription_leases`` container (see ``infra/modules/cosmos.bicep``) and the repository layer
  that reads it, not by this type. What this type *does* enforce is that a deployment cannot claim
  to be ``EXECUTING`` without naming the lease that entitles it to be.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundwork_contracts.audit import AuthorityChain

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class DeploymentStatus(StrEnum):
    """State machine for one execution attempt.

    There is no ``FAILED``. A deployment that cannot proceed is ``HALTED`` — FR-031b makes halted
    terminal only after the customer chooses retry, forward-fix, or rollback, so "failed and nobody
    has decided what happens next" is a real, addressable state, not a dead end. ``ROLLED_BACK`` is
    the terminal state that results from choosing rollback; it is not merely "halted, but worse".
    """

    QUEUED = "queued"
    EXECUTING = "executing"
    HALTED = "halted"
    SUCCEEDED = "succeeded"
    ROLLED_BACK = "rolled_back"


# Terminal in the sense that no further stage execution happens against this status without an
# explicit customer decision (FR-031b) — HALTED is included because "terminal" here means "not
# actively executing", not "closed". A halted deployment still has a completedAt: the *attempt*
# ended, even though the deployment as a whole awaits a decision.
_TERMINAL_STATUSES = frozenset(
    {DeploymentStatus.SUCCEEDED, DeploymentStatus.HALTED, DeploymentStatus.ROLLED_BACK}
)


class Checkpoint(BaseModel):
    """The last durable resume point for an execution attempt (FR-030, FR-035).

    ``resume_token`` is opaque here by design: what a stage needs to resume is entirely that stage's
    concern, and this type has no business knowing its shape. The one thing this type does enforce
    is that a checkpoint names *which* stage it belongs to and *when* it was recorded — the minimum
    needed to tell a stale checkpoint from a current one.

    ``resume_token`` deliberately allows the empty string — ``StageOutcome.resume_token`` (the only
    producer) defaults to ``""`` for exactly the stages that need no resume artefact at all (their
    own idempotent GET-before-write makes any retry a clean re-derivation, not a resume). Found live
    2026-08-25: a `min_length=1` constraint here made `record_checkpoint()` raise a
    `pydantic.ValidationError` on every single successful completion of `devops_project`/`fabric`/
    `validation_tests` (none of which set `resume_token`) — uncaught by `Sequencer.run()` (only the
    stage's own `execute()` is wrapped in try/except, not the checkpoint write after it), silently
    orphaning the deployment at `status=executing` forever with no halt, no log at any level below
    the outermost queue-loop handler, and no automatic recovery. See `AGENT_HANDOFF.md` §4 bug #7.
    """

    model_config = StrictModel

    stage_name: Annotated[str, Field(min_length=1)]
    resume_token: str
    recorded_at: datetime


class SubscriptionLease(BaseModel):
    """Proof of the right to execute against a subscription (FR-045b).

    A short-lived, renewable claim, not a permanent assignment. ``expires_at`` is what lets a worker
    that dies without releasing its lease fail to deadlock the subscription forever — a subsequent
    worker can take over once the lease lapses, per the ``subscription_leases`` container's 3600s
    default TTL (``infra/modules/cosmos.bicep``).
    """

    model_config = StrictModel

    holder: Annotated[str, Field(min_length=1)]
    expires_at: datetime

    def is_valid_at(self, moment: datetime) -> bool:
        return moment < self.expires_at


class Deployment(BaseModel):
    """Durable record of one execution attempt against an approved plan.

    ``authority`` mirrors :class:`~groundwork_contracts.audit.AuthorityChain` deliberately — a
    deployment's right to exist traces to the same plan-and-approval pair every audit record for it
    must cite. There is no separate, looser notion of "what this deployment is allowed to do".
    """

    model_config = StrictModel

    deployment_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    subscription_id: Annotated[str, Field(pattern=_GUID)]
    correlation_id: Annotated[str, Field(pattern=_GUID)]
    authority: AuthorityChain
    status: DeploymentStatus
    queue_position: Annotated[int, Field(ge=0)] | None = None
    current_stage: Annotated[str, Field(min_length=1)] | None = None
    checkpoint: Checkpoint | None = None
    lease: SubscriptionLease | None = None
    what_if_artefact_uri: Annotated[str, Field(min_length=1)] | None = None
    infrastructure_outputs: Annotated[str, Field(min_length=1)] | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None

    @model_validator(mode="after")
    def _executing_requires_a_lease(self) -> Self:
        # FR-045b's per-instance half: a deployment cannot assert it is currently executing without
        # naming the lease that entitles it to be. The *uniqueness* of that lease across concurrent
        # deployments is the repository layer's job (it reads the shared lease document); this is
        # the part a single instance can and must prove about itself.
        if self.status is DeploymentStatus.EXECUTING and self.lease is None:
            raise ValueError(
                "status is 'executing' but no lease is recorded; FR-045b requires an executing "
                "deployment to hold the subscription lease that authorises it"
            )
        return self

    @model_validator(mode="after")
    def _terminal_and_active_states_match_timestamps(self) -> Self:
        if self.status in _TERMINAL_STATUSES and self.completed_at is None:
            raise ValueError(
                f"status is {self.status.value!r} but completed_at is missing; a deployment that "
                f"is no longer active must record when it stopped being active"
            )
        if self.status is DeploymentStatus.QUEUED and self.completed_at is not None:
            raise ValueError(
                "status is 'queued' but completed_at is set; a queued deployment has not run"
            )
        if self.status is DeploymentStatus.QUEUED and self.started_at is not None:
            raise ValueError(
                "status is 'queued' but started_at is set; a queued deployment has not started"
            )
        if self.status is not DeploymentStatus.QUEUED and self.started_at is None:
            raise ValueError(
                f"status is {self.status.value!r} but started_at is missing; only a queued "
                f"deployment may have no start time"
            )
        return self

    @model_validator(mode="after")
    def _queue_position_only_while_queued(self) -> Self:
        if self.status is not DeploymentStatus.QUEUED and self.queue_position is not None:
            raise ValueError(
                f"status is {self.status.value!r} but queue_position is set; queue position is "
                f"meaningless once a deployment has started"
            )
        return self

    def is_active(self) -> bool:
        """Whether this deployment currently occupies a concurrency slot (FR-045a, FR-045b)."""
        return self.status in (DeploymentStatus.QUEUED, DeploymentStatus.EXECUTING)
