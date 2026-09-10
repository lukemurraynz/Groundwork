"""T073 — halt-and-preserve reconstruction, tested in isolation from the sequencer and the API.

See ``engine/halt.py``'s own module docstring for why the failing stage is reconstructed from
``DeploymentStageRecord`` history rather than stored directly on ``Deployment``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentStageRecord,
    IdempotenceOutcome,
    RecoveryPath,
    StageError,
    StageStatus,
)
from groundwork_contracts.deployment import Checkpoint, Deployment, DeploymentStatus
from groundwork_orchestrator.engine.halt import (
    build_stage_status_view,
    load_all_stage_records,
    reconstruct_halted_view,
    recovery_action_help,
    recovery_options_for_stage,
    requeue_after_recovery_choice,
)
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64
_RESUME_TOKEN = "ok"  # noqa: S105 -- an opaque stage-resume marker, not a credential

STAGE_ORDER = (
    "devops_project",
    "infrastructure",
    "identity",
    "networking",
    "fabric",
    "monitoring",
    "validation_tests",
)


@pytest.fixture
def blueprint():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


_record_counter = {"n": 0}


def _record(
    *,
    stage_name: str,
    attempt: int = 1,
    status: StageStatus = StageStatus.SUCCEEDED,
    ended_at: datetime | None = NOW,
    error: StageError | None = None,
    idempotence_outcome: IdempotenceOutcome | None = IdempotenceOutcome.APPLIED,
) -> DeploymentStageRecord:
    _record_counter["n"] += 1
    return DeploymentStageRecord(
        record_id=f"cccccccc-cccc-cccc-cccc-{_record_counter['n']:012d}",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        stage_name=stage_name,
        attempt=attempt,
        status=status,
        started_at=NOW,
        ended_at=ended_at,
        error=error,
        idempotence_outcome=(
            idempotence_outcome
            if status in (StageStatus.SUCCEEDED, StageStatus.SKIPPED_CONVERGED)
            else None
        ),
        recovery_path=RecoveryPath.FORWARD_FIX if status is StageStatus.FAILED else None,
    )


def _halted_deployment() -> Deployment:
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        status=DeploymentStatus.HALTED,
        current_stage=None,
        checkpoint=Checkpoint(
            stage_name="devops_project", resume_token=_RESUME_TOKEN, recorded_at=NOW
        ),
        started_at=NOW,
        completed_at=NOW,
    )


# --- recovery_options_for_stage --------------------------------------------------------------


def test_infrastructure_and_fabric_offer_rollback(blueprint) -> None:
    assert recovery_options_for_stage(blueprint, "infrastructure") == (
        "retry",
        "forward_fix",
        "rollback",
    )
    assert recovery_options_for_stage(blueprint, "fabric") == ("retry", "forward_fix", "rollback")


def test_other_stages_do_not_offer_rollback(blueprint) -> None:
    stage_names = ("devops_project", "identity", "networking", "monitoring", "validation_tests")
    for stage_name in stage_names:
        assert recovery_options_for_stage(blueprint, stage_name) == ("retry", "forward_fix")


# --- recovery_action_help ----------------------------------------------------------------------


def test_recovery_action_help_names_rollback_as_a_redeploy_not_a_teardown() -> None:
    """Customer-facing surfaces must not read `rollback` as a full teardown — it's a whole-stack
    redeploy to last-known-good via the customer's own pipeline (ADR-0009)."""
    help_text = recovery_action_help("rollback")
    assert "redeploy" in help_text.lower()
    assert "teardown" not in help_text.lower() or "not a teardown" in help_text.lower()


def test_recovery_action_help_covers_every_offered_action(blueprint) -> None:
    for action in recovery_options_for_stage(blueprint, "infrastructure"):
        assert recovery_action_help(action)


def test_recovery_action_help_unknown_action_returns_empty_string() -> None:
    assert recovery_action_help("not-a-real-action") == ""


# --- reconstruct_halted_view ------------------------------------------------------------------


def test_no_records_means_nothing_to_reconstruct(blueprint) -> None:
    assert reconstruct_halted_view([], blueprint) is None


def test_latest_record_succeeded_means_nothing_to_reconstruct(blueprint) -> None:
    records = [_record(stage_name="devops_project", status=StageStatus.SUCCEEDED)]
    assert reconstruct_halted_view(records, blueprint) is None


def test_reconstructs_the_failing_stage_error_and_recovery_options(blueprint) -> None:
    error = StageError(code="DeploymentStackFailed", message="quota exceeded", is_transient=False)
    records = [
        _record(stage_name="devops_project", status=StageStatus.SUCCEEDED, ended_at=NOW),
        _record(
            stage_name="infrastructure",
            status=StageStatus.FAILED,
            ended_at=NOW + timedelta(seconds=30),
            error=error,
        ),
    ]

    view = reconstruct_halted_view(records, blueprint)

    assert view is not None
    assert view.failing_stage == "infrastructure"
    assert view.error == error
    assert view.recovery_options == ("retry", "forward_fix", "rollback")


def test_picks_the_most_recently_ended_record_across_every_stage(blueprint) -> None:
    """A stage can fail, retry, and later succeed; a *different*, later stage can then fail and
    halt the deployment. The most-recently-ended record — not the most recent per stage_name — is
    the one that must win."""
    early_failure = StageError(code="Timeout", message="timed out", is_transient=True)
    later_failure = StageError(code="AuthzDenied", message="forbidden", is_transient=False)
    records = [
        _record(
            stage_name="devops_project",
            attempt=1,
            status=StageStatus.FAILED,
            ended_at=NOW,
            error=early_failure,
        ),
        _record(
            stage_name="devops_project",
            attempt=2,
            status=StageStatus.SUCCEEDED,
            ended_at=NOW + timedelta(seconds=5),
        ),
        _record(
            stage_name="infrastructure",
            status=StageStatus.FAILED,
            ended_at=NOW + timedelta(seconds=10),
            error=later_failure,
        ),
    ]

    view = reconstruct_halted_view(records, blueprint)

    assert view is not None
    assert view.failing_stage == "infrastructure"
    assert view.error == later_failure


# --- build_stage_status_view -------------------------------------------------------------------


def test_completed_stages_report_their_own_status_and_attempt(blueprint) -> None:
    records = [
        _record(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempt=1),
        _record(
            stage_name="infrastructure",
            status=StageStatus.SKIPPED_CONVERGED,
            idempotence_outcome=IdempotenceOutcome.NO_OP,
            attempt=1,
        ),
    ]

    views = build_stage_status_view(records, execution_order=STAGE_ORDER, current_stage=None)

    assert [v.stage_name for v in views] == ["devops_project", "infrastructure"]
    assert views[0].status == "succeeded"
    assert views[0].idempotence_outcome == "applied"
    assert views[1].status == "skipped_converged"
    assert views[1].idempotence_outcome == "no_op"


def test_unattempted_non_current_stages_are_omitted(blueprint) -> None:
    records = [_record(stage_name="devops_project", status=StageStatus.SUCCEEDED)]

    views = build_stage_status_view(records, execution_order=STAGE_ORDER, current_stage=None)

    assert [v.stage_name for v in views] == ["devops_project"]


def test_current_stage_with_no_record_yet_reports_started(blueprint) -> None:
    records = [_record(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempt=1)]

    views = build_stage_status_view(
        records, execution_order=STAGE_ORDER, current_stage="infrastructure"
    )

    assert views[-1].stage_name == "infrastructure"
    assert views[-1].status == "started"
    assert views[-1].idempotence_outcome is None
    assert views[-1].attempt == 1


def test_current_stage_started_attempt_accounts_for_prior_failed_attempts(blueprint) -> None:
    error = StageError(code="Timeout", message="timed out", is_transient=True)
    records = [
        _record(stage_name="fabric", status=StageStatus.FAILED, attempt=1, error=error),
        _record(stage_name="fabric", status=StageStatus.FAILED, attempt=2, error=error),
    ]

    views = build_stage_status_view(records, execution_order=STAGE_ORDER, current_stage="fabric")

    # Two prior FAILED attempts exist as records; a third attempt now in flight has no record of
    # its own yet, so it must report attempt=3, not attempt=1.
    assert views[-1].status == "started"
    assert views[-1].attempt == 3


def test_only_the_highest_attempt_record_is_reported_per_stage(blueprint) -> None:
    records = [
        _record(
            stage_name="devops_project",
            status=StageStatus.FAILED,
            attempt=1,
            error=StageError(code="Timeout", message="timed out", is_transient=True),
        ),
        _record(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempt=2),
    ]

    views = build_stage_status_view(records, execution_order=STAGE_ORDER, current_stage=None)

    assert len(views) == 1
    assert views[0].status == "succeeded"
    assert views[0].attempt == 2


# --- load_all_stage_records ---------------------------------------------------------------------


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def query_items(
        self, query: str = "", *, parameters: list[dict[str, Any]] | None = None, **_: Any
    ):
        for doc in self.documents.values():
            if parameters and not all(
                doc.get(p["name"].lstrip("@")) == p["value"] for p in parameters
            ):
                continue
            yield doc


async def test_load_all_stage_records_only_returns_this_deployments_records() -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    await repo.create(TENANT_ID, _record(stage_name="devops_project"))
    await repo.create(TENANT_ID, _record(stage_name="infrastructure"))
    other_deployment_record = DeploymentStageRecord(
        record_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        deployment_id="99999999-9999-9999-9999-999999999999",
        tenant_id=TENANT_ID,
        stage_name="devops_project",
        attempt=1,
        status=StageStatus.SUCCEEDED,
        started_at=NOW,
        ended_at=NOW,
    )
    await repo.create(TENANT_ID, other_deployment_record)

    records = await load_all_stage_records(repo, TENANT_ID, DEPLOYMENT_ID)

    assert {r.stage_name for r in records} == {"devops_project", "infrastructure"}
    assert all(r.deployment_id == DEPLOYMENT_ID for r in records)


# --- requeue_after_recovery_choice ---------------------------------------------------------------


def test_requeue_clears_every_field_a_queued_deployment_must_not_have() -> None:
    halted = _halted_deployment()

    requeued = requeue_after_recovery_choice(halted)

    assert requeued.status is DeploymentStatus.QUEUED
    assert requeued.current_stage is None
    assert requeued.started_at is None
    assert requeued.lease is None
    # The checkpoint is untouched — resuming from it is exactly the point (FR-030).
    assert requeued.checkpoint == halted.checkpoint


def test_requeue_result_satisfies_deployments_own_validators() -> None:
    """model_copy does not re-run validators (this codebase's established gotcha) — round-trip
    through model_validate to prove the result is genuinely valid, not just field-by-field
    plausible."""
    halted = _halted_deployment()

    requeued = requeue_after_recovery_choice(halted)

    revalidated = Deployment.model_validate(requeued.model_dump())
    assert revalidated.status is DeploymentStatus.QUEUED
