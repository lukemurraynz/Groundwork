"""Audit authority chain — the audit-by-construction rule, FR-047.

"Every mutating action traces to an approved plan and an authorising human" is only true if an
audit record without that chain cannot be constructed. These tests assert the type refuses, rather
than trusting every future call site to remember.

Also covers the report rules that stop a partial deployment reading as a success (FR-034, SC-015).
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from groundwork_contracts import (
    ActorIdentity,
    ActorType,
    AuditOutcome,
    AuditRecord,
    AuthorityChain,
    DeploymentReport,
    DeploymentStageRecord,
    IdempotenceOutcome,
    RecoveryPath,
    ReportOutcome,
    StageError,
    StageStatus,
    StageSummary,
)
from tests.conftest import (
    APPROVAL_ID,
    APPROVER_ID,
    CORRELATION_ID,
    DEPLOYMENT_ID,
    TENANT_ID,
)

pytestmark = pytest.mark.security

AUDIT_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
RECORD_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
REPORT_ID = "dddddddd-dddd-dddd-dddd-dddddddddddd"
PLAN_HASH = "sha256:" + "e" * 64
CONTENT_HASH = "sha256:" + "f" * 64


def _authority() -> AuthorityChain:
    return AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID)


def _actor(actor_type: ActorType = ActorType.WORKLOAD) -> ActorIdentity:
    return ActorIdentity(
        object_id=APPROVER_ID, display_name="Orchestrator Worker", actor_type=actor_type
    )


def _audit(now: datetime, **overrides: object) -> AuditRecord:
    kwargs: dict[str, object] = {
        "audit_id": AUDIT_ID,
        "tenant_id": TENANT_ID,
        "correlation_id": CORRELATION_ID,
        "deployment_id": DEPLOYMENT_ID,
        "sequence": 0,
        "occurred_at": now,
        "actor": _actor(),
        "action": "deploy_infrastructure",
        "is_mutating": True,
        "authorised_by": _authority(),
        "outcome": AuditOutcome.AUTHORISED,
        "retention_expires_at": now + timedelta(days=365),
    }
    kwargs.update(overrides)
    return AuditRecord(**kwargs)  # type: ignore[arg-type]


def test_mutating_action_without_authority_is_rejected(now: datetime) -> None:
    """FR-047 — an action that cannot be traced to an approved plan is a defect.

    Not an untracked success, not a warning. It cannot be written at all.
    """
    with pytest.raises(ValidationError, match="no authority chain"):
        _audit(now, authorised_by=None)


def test_read_only_action_needs_no_authority_chain(now: datetime) -> None:
    """Validation and planning read tenant state without an approval, by design."""
    record = _audit(
        now,
        action="validate_landing_zone",
        is_mutating=False,
        authorised_by=None,
        outcome=AuditOutcome.SUCCEEDED,
    )
    assert record.authorised_by is None


def test_agent_cannot_perform_a_mutating_action(now: datetime) -> None:
    """The deterministic-execution boundary — if a mutating action is ever attributed to an
    agent, that is a breach.

    ``AGENT`` is representable precisely so this fails by name rather than as a vague schema error.
    """
    with pytest.raises(ValidationError, match="attributed to an agent"):
        _audit(now, actor=_actor(ActorType.AGENT))


def test_agent_may_perform_a_read_only_action(now: datetime) -> None:
    record = _audit(
        now,
        actor=_actor(ActorType.AGENT),
        action="generate_deployment_plan",
        is_mutating=False,
        authorised_by=None,
        outcome=AuditOutcome.SUCCEEDED,
    )
    assert record.actor.actor_type is ActorType.AGENT


def test_audit_retention_is_capped(now: datetime) -> None:
    """FR-052a — 12 months, matching conversation retention."""
    with pytest.raises(ValidationError, match="exceeds the 365-day period"):
        _audit(now, retention_expires_at=now + timedelta(days=800))


def test_audit_record_is_immutable(now: datetime) -> None:
    """Append-only means no in-place edit, or the trail is not evidence."""
    record = _audit(now)
    with pytest.raises(ValidationError):
        record.outcome = AuditOutcome.DENIED  # type: ignore[misc]


# --- Stage records ----------------------------------------------------------------


def _stage(now: datetime, **overrides: object) -> DeploymentStageRecord:
    kwargs: dict[str, object] = {
        "record_id": RECORD_ID,
        "deployment_id": DEPLOYMENT_ID,
        "tenant_id": TENANT_ID,
        "stage_name": "infrastructure",
        "attempt": 1,
        "status": StageStatus.SUCCEEDED,
        "started_at": now,
        "ended_at": now + timedelta(minutes=8),
        "idempotence_outcome": IdempotenceOutcome.APPLIED,
    }
    kwargs.update(overrides)
    return DeploymentStageRecord(**kwargs)  # type: ignore[arg-type]


def test_failed_stage_must_declare_a_recovery_path(now: datetime) -> None:
    """FR-031 — a stage with no recovery path must not be marked complete."""
    with pytest.raises(ValidationError, match="declares no recovery path"):
        _stage(
            now,
            status=StageStatus.FAILED,
            error=StageError(
                code="QuotaExceeded", message="vCPU quota exhausted", is_transient=False
            ),
        )


def test_failed_stage_must_record_an_error(now: datetime) -> None:
    with pytest.raises(ValidationError, match="records no error"):
        _stage(now, status=StageStatus.FAILED, recovery_path=RecoveryPath.FORWARD_FIX)


def test_converged_stage_must_be_a_no_op(now: datetime) -> None:
    """FR-029 — re-execution against converged state is a no-op.

    A stage reporting converged state while claiming it applied changes is contradicting itself.
    """
    with pytest.raises(ValidationError, match="not 'no_op'"):
        _stage(
            now,
            status=StageStatus.SKIPPED_CONVERGED,
            idempotence_outcome=IdempotenceOutcome.APPLIED,
        )


def test_converged_no_op_is_accepted(now: datetime) -> None:
    record = _stage(
        now,
        status=StageStatus.SKIPPED_CONVERGED,
        idempotence_outcome=IdempotenceOutcome.NO_OP,
    )
    assert record.idempotence_outcome is IdempotenceOutcome.NO_OP


def test_started_stage_has_no_end_time(now: datetime) -> None:
    with pytest.raises(ValidationError, match="'started' but has an ended_at"):
        _stage(now, status=StageStatus.STARTED)


def test_terminal_stage_requires_an_end_time(now: datetime) -> None:
    with pytest.raises(ValidationError, match="has no ended_at"):
        _stage(now, status=StageStatus.SUCCEEDED, ended_at=None)


# --- Reports ----------------------------------------------------------------------


def _report(now: datetime, **overrides: object) -> DeploymentReport:
    kwargs: dict[str, object] = {
        "report_id": REPORT_ID,
        "deployment_id": DEPLOYMENT_ID,
        "tenant_id": TENANT_ID,
        "correlation_id": CORRELATION_ID,
        "authority": _authority(),
        "outcome": ReportOutcome.SUCCEEDED,
        "stage_summary": (
            StageSummary(stage_name="infrastructure", status=StageStatus.SUCCEEDED, attempts=1),
        ),
        "final_monthly_cost_aud": 412.50,
        "blob_uri": "https://example.invalid/reports/1",
        "content_hash": CONTENT_HASH,
        "generated_at": now,
        "retention_expires_at": now + timedelta(days=365),
    }
    kwargs.update(overrides)
    return DeploymentReport(**kwargs)  # type: ignore[arg-type]


def test_partial_deployment_cannot_be_reported_as_success(now: datetime) -> None:
    """FR-034 and SC-015 — the failure must not be moved to the customer as a success."""
    with pytest.raises(ValidationError, match="claims success but stage"):
        _report(
            now,
            stage_summary=(
                StageSummary(
                    stage_name="infrastructure",
                    status=StageStatus.SUCCEEDED,
                    attempts=1,
                ),
                StageSummary(stage_name="fabric", status=StageStatus.FAILED, attempts=3),
            ),
        )


def test_never_ran_stage_blocks_a_success_claim(now: datetime) -> None:
    """SC-015 — a report must state which stages never ran, and not imply they were fine."""
    with pytest.raises(ValidationError, match="claims success but stage"):
        _report(
            now,
            stage_summary=(
                StageSummary(
                    stage_name="infrastructure",
                    status=StageStatus.SUCCEEDED,
                    attempts=1,
                ),
                StageSummary(stage_name="monitoring", never_ran=True),
            ),
        )


def test_halted_report_may_contain_failures(now: datetime) -> None:
    """Halt-and-preserve (FR-031) — completed stages are reported, not discarded."""
    report = _report(
        now,
        outcome=ReportOutcome.HALTED,
        stage_summary=(
            StageSummary(stage_name="infrastructure", status=StageStatus.SUCCEEDED, attempts=1),
            StageSummary(stage_name="fabric", status=StageStatus.FAILED, attempts=3),
            StageSummary(stage_name="monitoring", never_ran=True),
        ),
    )
    assert report.outcome is ReportOutcome.HALTED
    assert any(s.never_ran for s in report.stage_summary)


def test_never_ran_stage_cannot_also_carry_a_status(now: datetime) -> None:
    with pytest.raises(ValidationError, match="never_ran but carries a status"):
        StageSummary(stage_name="monitoring", never_ran=True, status=StageStatus.SUCCEEDED)


def test_stage_summary_needs_a_status_or_never_ran(now: datetime) -> None:
    with pytest.raises(ValidationError, match="not marked never_ran"):
        StageSummary(stage_name="monitoring")


def test_expired_report_is_distinguishable(now: datetime) -> None:
    """FR-052b — an expired report must not read as "the deployment never happened"."""
    report = _report(now)
    assert report.is_expired_at(now) is False
    assert report.is_expired_at(now + timedelta(days=366)) is True


def test_report_retention_is_capped(now: datetime) -> None:
    with pytest.raises(ValidationError, match="exceeds the 365-day period"):
        _report(now, retention_expires_at=now + timedelta(days=800))
