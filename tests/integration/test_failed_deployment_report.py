"""T089 — failed-deployment report content and the structural guarantee (SC-015).

Prove the full chain — Sequencer halts → ``build_report_content`` assembles a stage-accurate
view → ``ReportArchiveStore.archive`` persists it → a read-back passes a second
``DeploymentReport.model_validate`` — agrees end to end, and that constructing a
``DeploymentReport`` with ``outcome=SUCCEEDED`` and a failed/never-ran stage is structurally
rejected by Pydantic's own validator, not just by convention.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pydantic
import pytest
from tests.unit.test_report_archive import _FakeContainerClient

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    DeploymentStageRecord,
    RecoveryPath,
    ReportOutcome,
    StageError,
    StageStatus,
    StageSummary,
)
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_orchestrator.engine.report_builder import build_report_content
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_orchestrator.state.report_archive import ReportArchiveStore
from groundwork_shared.config.blueprints import load_blueprint

pytestmark = pytest.mark.integration

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
    """Matches the filtering ``query_items`` pattern from ``test_report_builder.py`` —
    a naive fake that ignores ``parameters`` has caused real, hard-to-diagnose test failures
    in this project before (T072's fix, recorded in the project's internal implementation
    notes, not included in this release).

    ``upsert_item`` and ``read_item`` are included to satisfy the ``ContainerLike`` protocol
    ``TenantScopedRepository`` expects — these are not exercised by the report builder path
    but are required for the repository to be constructed at all."""

    from azure.cosmos.exceptions import CosmosResourceNotFoundError

    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        found = self.documents.get(item)
        if found is None:
            raise self.CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

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
    return Deployment(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        status=status,
        started_at=NOW,
        completed_at=NOW + timedelta(minutes=10),
    )


async def test_failed_deployment_reports_failed_and_never_ran_stages(blueprint, valid_plan) -> None:
    """SC-015: a halted deployment's report must correctly distinguish succeeded, failed,
    and never-ran stages — and must never be reported as SUCCEEDED."""
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    # devops_project succeeded.
    await repo.create(
        TENANT_ID, _record(stage_name="devops_project", resources_affected=("proj-1",))
    )
    # infrastructure failed — the halt trigger.
    await repo.create(
        TENANT_ID,
        _record(stage_name="infrastructure", status=StageStatus.FAILED, resources_affected=()),
    )
    # Everything after infrastructure never attempted.

    content = await build_report_content(
        _deployment(status=DeploymentStatus.HALTED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    assert content.outcome is ReportOutcome.HALTED, (
        "a halted deployment must never be reported as succeeded"
    )
    by_name = {s.stage_name: s for s in content.stage_summary}
    assert by_name["devops_project"].status is StageStatus.SUCCEEDED
    assert by_name["devops_project"].never_ran is False
    assert by_name["infrastructure"].status is StageStatus.FAILED
    assert by_name["infrastructure"].never_ran is False
    # Every stage after the failing one is never_ran.
    failed_index = STAGE_ORDER.index("infrastructure")
    for stage_name in STAGE_ORDER[failed_index + 1 :]:
        assert by_name[stage_name].never_ran is True, (
            f"stage {stage_name!r} must report never_ran, since infrastructure failed before it "
            f"could start"
        )
        assert by_name[stage_name].status is None, (
            f"stage {stage_name!r} is marked never_ran and must carry no status"
        )


async def test_end_to_end_archive_of_failed_report_is_valid_and_halted(
    blueprint, valid_plan
) -> None:
    """The full chain — builder → archive → read-back → model_validate — agrees: outcome is
    HALTED, and the report passes every Pydantic validator on re-validation."""
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    await repo.create(
        TENANT_ID, _record(stage_name="devops_project", resources_affected=("proj-1",))
    )
    await repo.create(
        TENANT_ID,
        _record(stage_name="infrastructure", status=StageStatus.FAILED),
    )

    content = await build_report_content(
        _deployment(status=DeploymentStatus.HALTED),
        valid_plan,
        stage_record_repository=repo,
        blueprint=blueprint,
    )

    blob_container = _FakeContainerClient()
    archive_store = ReportArchiveStore(blob_container)
    report = await archive_store.archive(content, now=NOW)

    # Read-back round-trips through model_validate (re-runs validators).
    blob_client = blob_container.clients[f"{report.report_id}.json"]
    assert blob_client.uploaded is not None
    re_validated = DeploymentReport.model_validate(json.loads(blob_client.uploaded.decode("utf-8")))
    assert re_validated.outcome is ReportOutcome.HALTED, (
        "the persisted report claims an outcome other than HALTED for a halted deployment"
    )


def test_succeeded_report_rejects_a_failed_or_never_ran_stage() -> None:
    """The ``DeploymentReport._success_means_every_stage_succeeded`` validator is structural,
    not advisory — constructing a report with ``outcome=SUCCEEDED`` and an incomplete stage
    must raise ``pydantic.ValidationError``, regardless of which code path called it."""
    # Construct the exact StageSummary the builder would produce for a halted deployment.
    summaries = [
        StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
        StageSummary(stage_name="infrastructure", status=StageStatus.FAILED, attempts=1),
        StageSummary(stage_name="identity", never_ran=True, attempts=0),
    ]
    with pytest.raises(pydantic.ValidationError) as exc_info:
        DeploymentReport(
            report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            deployment_id=DEPLOYMENT_ID,
            tenant_id=TENANT_ID,
            correlation_id=CORRELATION_ID,
            authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
            outcome=ReportOutcome.SUCCEEDED,
            stage_summary=tuple(summaries),
            resources_created=("proj-1",),
            iac_artefact_versions={"standard-production-fabric": "1.0.0"},
            final_monthly_cost_aud=412.50,
            blob_uri="https://example.invalid/reports/dddddddd.json",
            content_hash="sha256:" + "0" * 64,
            generated_at=NOW,
            retention_expires_at=NOW + timedelta(days=365),
        )
    errors = exc_info.value.errors()
    assert any("did not complete" in str(e.get("msg", "")) for e in errors), (
        "the validator did not reject a SUCCEEDED report with a failed and never-ran stage; "
        "this structural guarantee must hold for any caller, not just the known code paths"
    )
