"""T092 — report content assembly (FR-034, FR-052, SC-015)."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from groundwork_contracts.audit import (
    DeploymentStageRecord,
    RecoveryPath,
    ReportOutcome,
    StageError,
    StageStatus,
)
from groundwork_contracts.deployment import AuthorityChain, Deployment, DeploymentStatus
from groundwork_orchestrator.engine.report_builder import NotReportableError, build_report_content
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64

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


_record_counter = {"n": 0}


def _record(
    *,
    stage_name: str,
    attempt: int = 1,
    status: StageStatus = StageStatus.SUCCEEDED,
    resources_affected: tuple[str, ...] = (),
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
        ended_at=NOW + timedelta(seconds=5),
        resources_affected=resources_affected,
        error=(
            StageError(code="Failed", message="stage failed", is_transient=False)
            if status is StageStatus.FAILED
            else None
        ),
        recovery_path=RecoveryPath.FORWARD_FIX if status is StageStatus.FAILED else None,
    )


def _deployment(*, status: DeploymentStatus) -> Deployment:
    is_queued = status is DeploymentStatus.QUEUED
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        status=status,
        started_at=None if is_queued else NOW,
        completed_at=None if is_queued else NOW + timedelta(minutes=10),
    )


async def test_halted_deployment_reports_succeeded_failed_and_never_ran(
    blueprint, valid_plan
) -> None:
    """SC-015: the report must distinguish all three — not just succeeded vs. everything else."""
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    await repo.create(
        TENANT_ID, _record(stage_name="devops_project", resources_affected=("proj-1",))
    )
    await repo.create(
        TENANT_ID,
        _record(
            stage_name="infrastructure",
            status=StageStatus.FAILED,
            resources_affected=(),
        ),
    )
    # identity, networking, fabric, monitoring, validation_tests never attempted.

    content = await build_report_content(
        _deployment(status=DeploymentStatus.HALTED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    assert content.outcome is ReportOutcome.HALTED
    by_name = {s.stage_name: s for s in content.stage_summary}
    assert by_name["devops_project"].status is StageStatus.SUCCEEDED
    assert by_name["devops_project"].never_ran is False
    assert by_name["infrastructure"].status is StageStatus.FAILED
    assert by_name["identity"].never_ran is True
    assert by_name["identity"].status is None
    assert by_name["validation_tests"].never_ran is True
    # Every real blueprint stage is represented, none silently omitted.
    assert set(by_name) == set(STAGE_ORDER)


async def test_succeeded_deployment_reports_all_stages_succeeded(blueprint, valid_plan) -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    for name in STAGE_ORDER:
        await repo.create(TENANT_ID, _record(stage_name=name, resources_affected=(f"res-{name}",)))

    content = await build_report_content(
        _deployment(status=DeploymentStatus.SUCCEEDED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    assert content.outcome is ReportOutcome.SUCCEEDED
    assert all(not s.never_ran for s in content.stage_summary)
    assert content.resources_created == tuple(sorted(f"res-{name}" for name in STAGE_ORDER))


async def test_only_the_highest_attempt_counts_but_attempts_are_totalled(
    blueprint, valid_plan
) -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    await repo.create(
        TENANT_ID, _record(stage_name="devops_project", attempt=1, status=StageStatus.FAILED)
    )
    await repo.create(
        TENANT_ID, _record(stage_name="devops_project", attempt=2, status=StageStatus.SUCCEEDED)
    )

    content = await build_report_content(
        _deployment(status=DeploymentStatus.SUCCEEDED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    devops = next(s for s in content.stage_summary if s.stage_name == "devops_project")
    assert devops.status is StageStatus.SUCCEEDED
    assert devops.attempts == 2


async def test_final_cost_and_iac_versions_come_from_the_plan_and_blueprint(
    blueprint, valid_plan
) -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )

    content = await build_report_content(
        _deployment(status=DeploymentStatus.SUCCEEDED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    assert content.final_monthly_cost_aud == valid_plan.cost_estimate.monthly_total
    assert content.iac_artefact_versions == {blueprint.blueprint_id: blueprint.version}


async def test_non_terminal_status_is_not_reportable(blueprint, valid_plan) -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )

    with pytest.raises(NotReportableError):
        await build_report_content(
            _deployment(status=DeploymentStatus.QUEUED),
            valid_plan,
            stage_record_repository=repo,
            blueprint=blueprint,
        )
