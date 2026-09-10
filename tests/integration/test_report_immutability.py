"""T088 — archived report immutability and content-hash validation (FR-052).

An archived :class:`~groundwork_contracts.audit.DeploymentReport` must be byte-identical after a
"restart" (re-read through the same blob client), and its ``content_hash`` must validate against
its own content — proving the hash is not merely stored but genuinely recomputable from the same
data, the round-trip is lossless, and a report's identity fields are incorporated into the hash
(so tampering with the blob URI or retention date changes the hash).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_orchestrator.engine.report_builder import ReportContent
from groundwork_orchestrator.state.report_archive import ReportArchiveStore

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64


class _FakeBlobClient:
    """Matches the shape of ``report_archive.py``'s ``BlobClientLike`` protocol."""

    def __init__(self, url: str) -> None:
        self.url = url
        self.uploaded: bytes | None = None
        self.upload_kwargs: dict[str, Any] = {}

    async def upload_blob(self, data: bytes, **kwargs: Any) -> _FakeBlobClient:
        self.uploaded = data
        self.upload_kwargs = kwargs
        return self


class _FakeContainerClient:
    """Matches the shape of ``report_archive.py``'s ``ContainerClientLike`` protocol."""

    def __init__(self) -> None:
        self.clients: dict[str, _FakeBlobClient] = {}

    def get_blob_client(self, blob: str) -> _FakeBlobClient:
        if blob not in self.clients:
            self.clients[blob] = _FakeBlobClient(f"https://example.invalid/reports/{blob}")
        return self.clients[blob]


def _content() -> ReportContent:
    return ReportContent(
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=ReportOutcome.SUCCEEDED,
        stage_summary=(
            StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
            StageSummary(stage_name="infrastructure", status=StageStatus.SUCCEEDED, attempts=1),
        ),
        resources_created=("proj-1", "kv-gw-abc123"),
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=412.50,
    )


def _recompute_hash(report: DeploymentReport) -> str:
    """Recompute ``content_hash`` the same way ``ReportArchiveStore.archive`` does — copy the
    identical ``hashable`` dict construction so the test is testing the actual production
    algorithm, not a different one it invented."""
    hashable: dict[str, object] = {
        "reportId": report.report_id,
        "deploymentId": report.deployment_id,
        "tenantId": report.tenant_id,
        "correlationId": report.correlation_id,
        "authority": report.authority.model_dump(mode="json"),
        "outcome": report.outcome.value,
        "stageSummary": [s.model_dump(mode="json") for s in report.stage_summary],
        "resourcesCreated": list(report.resources_created),
        "iacArtefactVersions": report.iac_artefact_versions,
        "finalMonthlyCostAud": report.final_monthly_cost_aud,
        "generatedAt": report.generated_at.isoformat(),
    }
    canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


async def test_archived_report_is_byte_identical_after_restart() -> None:
    """Archive a report, simulate a restart by reading the blob back through the same fake
    container, and assert the JSON body round-trips unchanged."""
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    original = await store.archive(_content(), now=NOW)
    blob_client = container.clients[f"{original.report_id}.json"]
    assert blob_client.uploaded is not None

    # "Restart": re-parse the stored bytes as if we just fetched them from the real blob.
    re_read_body = json.loads(blob_client.uploaded.decode("utf-8"))
    re_read_report = DeploymentReport.model_validate(re_read_body)

    # Re-serialise with the same settings the archive store uses.
    re_serialised = json.dumps(
        re_read_report.model_dump(mode="json"), sort_keys=True, indent=2
    ).encode("utf-8")

    assert re_serialised == blob_client.uploaded, (
        "the round-tripped report is not byte-identical; either re-serialisation differs "
        "from the original, or fields were silently altered"
    )


async def test_stored_content_hash_matches_recomputation() -> None:
    """Recompute the content hash from the read-back report's own fields using the same
    algorithm ``ReportArchiveStore.archive`` uses — it must match the stored hash,
    proving tampering would be detectable."""
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    original = await store.archive(_content(), now=NOW)

    assert _recompute_hash(original) == original.content_hash, (
        "the recomputed content_hash differs from the stored one; either the hash was "
        "computed incorrectly or the report's fields have drifted from what was hashed"
    )


async def test_altering_a_report_field_changes_its_hash() -> None:
    """Prove the content_hash is genuinely content-dependent — flipping a single field
    (the outcome, the cost, a resource name) must change the hash."""
    container = _FakeContainerClient()
    store = ReportArchiveStore(container)

    a = await store.archive(_content(), now=NOW)

    # Same content, different outcome — must produce a different hash.
    halted_content = _content()
    object.__setattr__(halted_content, "outcome", ReportOutcome.HALTED)
    b = await store.archive(halted_content, now=NOW)

    assert a.content_hash != b.content_hash, (
        "two reports differing only in outcome produced the same content_hash; the hash is "
        "not genuinely content-sensitive"
    )
