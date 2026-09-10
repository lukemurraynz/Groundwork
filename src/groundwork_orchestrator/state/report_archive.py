"""Immutable report archival (T093; FR-052, FR-052a, SC-015).

Writes a finished deployment's :class:`~groundwork_contracts.audit.DeploymentReport` to the
``reports`` immutable blob container (``infra/modules/storage.bicep`` — the same time-based
immutability policy and 12-month retention as ``approvals``/``previews``), computing the report's
own content hash so a later reader can detect any post-hoc alteration (FR-052).

Same split every other Azure-writing module in this session uses: a thin
``ContainerClientLike``/``BlobClientLike`` protocol plus a real ``build_report_archive_store``
factory, so ``ReportArchiveStore.archive`` is unit-testable with no live storage account behind it —
see ``approval/artefacts.py`` (the original of this pattern) or ``engine/preview.py``'s
``WhatIfPreviewStore`` (the most recent).

**Why this computes ``content_hash`` itself rather than ``report_builder.py`` doing it.**
:class:`~groundwork_contracts.audit.DeploymentReport` is self-referential — ``content_hash`` is a
field *on* the report, hashing the report's *other* fields. Unlike
:class:`~groundwork_contracts.plan.SealedDeploymentPlan` (whose ``plan_hash`` hashes a *different*,
nested object with no circularity at all), a `DeploymentReport` cannot exist before its own hash is
known, and the hash cannot be computed before every other field — including the storage identity
(``report_id``, ``blob_uri``, ``retention_expires_at``) only this module knows how to mint — is
decided. So the two-phase order lives here: mint identity, hash the content plus that identity,
construct the one real, fully-valid `DeploymentReport`, then write it.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

from groundwork_contracts.audit import DeploymentReport
from groundwork_orchestrator.engine.report_builder import ReportContent

RETENTION_DAYS = 365
"""FR-052a: 12 months, matching the audit and conversation retention period this codebase already
uses elsewhere (`Sequencer.AUDIT_RETENTION_DAYS`, `ConversationRecord.ttlSeconds`)."""


class BlobClientLike(Protocol):
    # Matches the real azure.storage.blob.aio return shape (a plain dict of blob properties, not
    # an object with .url) — see engine/preview.py's own WhatIfPreviewStore.store() for the real
    # bug this codebase hit when a sibling module's Protocol claimed otherwise. This module's own
    # archive() already never reads .url off the upload result (uses blob_url_for() instead), so
    # this was a type-annotation inaccuracy, not a runtime bug — fixed for consistency.
    async def upload_blob(self, data: bytes, **kwargs: Any) -> dict[str, Any]: ...

    @property
    def url(self) -> str: ...


class ContainerClientLike(Protocol):
    def get_blob_client(self, blob: str) -> BlobClientLike: ...


class ReportArchiveStoreLike(Protocol):
    """What ``Sequencer._generate_report`` depends on — injectable so a test can supply a fake
    with no blob client behind it, the same seam ``WhatIfCaptureLike`` (``engine/preview.py``)
    already established."""

    async def archive(self, content: ReportContent, *, now: datetime) -> DeploymentReport: ...


class ReportArchiveStore:
    """Writes one immutable JSON blob per report into the ``reports`` container."""

    def __init__(self, container: ContainerClientLike) -> None:
        self._container = container

    def blob_url_for(self, report_id: str) -> str:
        """The URL ``archive`` will upload to, without performing any I/O — the same
        deterministic-URL-before-write pattern ``approval/artefacts.py``'s own
        ``blob_url_for`` uses, so a report that somehow failed its own validation would never
        trigger a wasted Azure write."""
        return self._container.get_blob_client(f"{report_id}.json").url

    async def archive(self, content: ReportContent, *, now: datetime) -> DeploymentReport:
        report_id = str(uuid.uuid4())
        blob_uri = self.blob_url_for(report_id)
        retention_expires_at = now + timedelta(days=RETENTION_DAYS)

        hashable = {
            "reportId": report_id,
            "deploymentId": content.deployment_id,
            "tenantId": content.tenant_id,
            "correlationId": content.correlation_id,
            "authority": content.authority.model_dump(mode="json"),
            "outcome": content.outcome.value,
            "stageSummary": [s.model_dump(mode="json") for s in content.stage_summary],
            "resourcesCreated": list(content.resources_created),
            "iacArtefactVersions": content.iac_artefact_versions,
            "finalMonthlyCostAud": content.final_monthly_cost_aud,
            "generatedAt": now.isoformat(),
        }
        canonical = json.dumps(hashable, sort_keys=True, separators=(",", ":"))
        content_hash = "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

        report = DeploymentReport(
            report_id=report_id,
            deployment_id=content.deployment_id,
            tenant_id=content.tenant_id,
            correlation_id=content.correlation_id,
            authority=content.authority,
            outcome=content.outcome,
            stage_summary=content.stage_summary,
            resources_created=content.resources_created,
            iac_artefact_versions=content.iac_artefact_versions,
            final_monthly_cost_aud=content.final_monthly_cost_aud,
            blob_uri=blob_uri,
            content_hash=content_hash,
            generated_at=now,
            retention_expires_at=retention_expires_at,
        )

        blob_client = self._container.get_blob_client(f"{report_id}.json")
        body = json.dumps(report.model_dump(mode="json"), sort_keys=True, indent=2).encode("utf-8")
        await blob_client.upload_blob(body, overwrite=False, content_type="application/json")
        return report


def build_report_archive_store(*, storage_account_url: str, credential: Any) -> ReportArchiveStore:
    """Construct a real :class:`ReportArchiveStore` against the ``reports`` container. The only
    place ``BlobServiceClient`` is constructed for this module."""
    from azure.storage.blob.aio import BlobServiceClient

    service_client = BlobServiceClient(account_url=storage_account_url, credential=credential)
    container_client = service_client.get_container_client("reports")
    return ReportArchiveStore(container_client)  # type: ignore[arg-type]
