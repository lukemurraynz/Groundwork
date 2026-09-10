"""T093 — immutable report archival (FR-052, FR-052a)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from groundwork_contracts.audit import AuthorityChain, ReportOutcome, StageStatus, StageSummary
from groundwork_orchestrator.engine.report_builder import ReportContent
from groundwork_orchestrator.state.report_archive import RETENTION_DAYS, ReportArchiveStore

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64


class _FakeBlobClient:
    def __init__(self, url: str) -> None:
        self.url = url
        self.uploaded: bytes | None = None
        self.upload_kwargs: dict[str, Any] = {}

    async def upload_blob(self, data: bytes, **kwargs: Any) -> _FakeBlobClient:
        self.uploaded = data
        self.upload_kwargs = kwargs
        return self


class _FakeContainerClient:
    def __init__(self) -> None:
        self.clients: dict[str, _FakeBlobClient] = {}

    def get_blob_client(self, blob: str) -> _FakeBlobClient:
        if blob not in self.clients:
            self.clients[blob] = _FakeBlobClient(f"https://example.invalid/reports/{blob}")
        return self.clients[blob]


def _content(outcome: ReportOutcome = ReportOutcome.SUCCEEDED) -> ReportContent:
    return ReportContent(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=outcome,
        stage_summary=(
            StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
        ),
        resources_created=("proj-1",),
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=412.50,
    )


async def test_archive_produces_a_valid_report_with_computed_identity() -> None:
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    report = await store.archive(_content(), now=NOW)

    assert report.deployment_id == DEPLOYMENT_ID
    assert report.content_hash.startswith("sha256:")
    assert report.blob_uri == f"https://example.invalid/reports/{report.report_id}.json"
    assert report.retention_expires_at == NOW + timedelta(days=RETENTION_DAYS)
    assert report.generated_at == NOW


async def test_archive_uploads_the_report_body_to_the_deterministic_url() -> None:
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    report = await store.archive(_content(), now=NOW)

    blob_client = container.clients[f"{report.report_id}.json"]
    assert blob_client.uploaded is not None
    assert blob_client.upload_kwargs["content_type"] == "application/json"
    assert blob_client.upload_kwargs["overwrite"] is False


async def test_blob_url_for_matches_the_url_actually_used() -> None:
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    report = await store.archive(_content(), now=NOW)

    assert store.blob_url_for(report.report_id) == report.blob_uri


async def test_two_reports_for_different_content_have_different_hashes() -> None:
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    succeeded = await store.archive(_content(ReportOutcome.SUCCEEDED), now=NOW)
    halted = await store.archive(_content(ReportOutcome.HALTED), now=NOW)

    assert succeeded.content_hash != halted.content_hash
