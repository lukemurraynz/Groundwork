"""``GET /deployments/{deploymentId}/report`` (T095; FR-052, FR-052b, SC-015).

Reads the ``reports`` Cosmos metadata document ``Sequencer._generate_report`` writes the moment a
deployment reaches a terminal, reportable state (``SUCCEEDED`` or ``HALTED`` — see
``engine/report_builder.py``'s own module docstring). This route does no assembly of its own; it
is a pure read, matching ``GET /deployments/{deploymentId}`` (``api/deployments.py``) and
``GET /plans/{planId}`` (``api/plans.py``)'s own shape.

**404 vs. 410, per FR-052b.** A deployment that has not yet reached a terminal state — still
``QUEUED``/``EXECUTING`` — has genuinely no report yet: ``404``. A report that exists but whose
``retentionExpiresAt`` has passed must say so explicitly, not report ``404`` as if it never
existed: ``410``. The one thing the immutability policy on the ``reports`` blob container
(``infra/modules/storage.bicep``) does not do is expire the *Cosmos metadata* alongside the blob
lifecycle rule — this check is what makes FR-052b's "explicit expired response" true at the API
layer as well as the storage layer.

**``blobUri`` carries a fresh, time-limited User Delegation SAS, minted at response time.**
The stored ``DeploymentReport.blob_uri`` is still the permanent, deterministic blob URL
(``ReportArchiveStore.blob_url_for``) — that stability is required for a 365-day-retention audit
record. What changed (WAF assessment §2.7): this route no longer echoes that permanent URL
verbatim to every caller. ``groundwork_shared.storage.sas.read_only_sas_url`` mints a 24-hour,
Entra-backed, secretless SAS from it on every read, so a URL that leaves this response and is later
found in a log line or forwarded message stops granting access within a day, instead of forever.

**T094/FR-004c: content negotiation for an accessible rendering.** A caller sending
``Accept: text/html`` gets ``report_render.py``'s WCAG 2.2 AA HTML rendering instead of the JSON
body below — the same report, same 404/410 rules, different representation. Every other client
(the default, and anything sending ``application/json`` or ``*/*``) is unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from groundwork_controlplane.api.auth import AuthenticatedCaller
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_orchestrator.state.report_render import render_report_html
from groundwork_shared.storage.sas import read_only_sas_url

router = APIRouter(prefix="/v1", tags=["deployments"])


def _now(request: Request) -> datetime:
    """Injectable via ``app.state.now_fn`` — same discipline as ``api/approvals.py``'s own
    ``_now``, so a retention-expiry test can seal a fixed clock rather than depend on real
    wall-clock time actually passing."""
    now_fn = getattr(request.app.state, "now_fn", None)
    return now_fn() if now_fn is not None else datetime.now(UTC)


def _wants_html(request: Request) -> bool:
    """Lightweight content negotiation — not full RFC 7231 q-value parsing, just enough to tell a
    browser's default ``Accept`` header (``text/html`` listed first) from an API client's
    (``application/json`` or no header at all)."""
    accept = request.headers.get("accept", "")
    return "text/html" in accept.split(",")[0]


@router.get("/deployments/{deployment_id}/report", response_model=None)
async def get_report(
    deployment_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object] | HTMLResponse:
    report_repository = request.app.state.report_repository
    report = await report_repository.read(caller.tenant_id, deployment_id)
    if report is None:
        raise HTTPException(status_code=404, detail="report not yet generated")
    if report.retention_expires_at <= _now(request):
        raise HTTPException(
            status_code=410,
            detail="report retention period has expired (FR-052a); the archived blob is no "
            "longer retrievable",
        )

    if _wants_html(request):
        return HTMLResponse(content=render_report_html(report))

    blob_uri = await read_only_sas_url(report.blob_uri, credential=request.app.state.credential)
    return {
        "reportId": report.report_id,
        "deploymentId": report.deployment_id,
        "blobUri": blob_uri,
        "retentionExpiresAt": report.retention_expires_at.isoformat(),
        "contentHash": report.content_hash,
        "outcome": report.outcome.value,
        "stageSummary": [
            {
                "stageName": s.stage_name,
                "status": s.status.value if s.status is not None else None,
                "neverRan": s.never_ran,
                "attempts": s.attempts,
            }
            for s in report.stage_summary
        ],
        "resourcesCreated": list(report.resources_created),
        "iacArtefactVersions": report.iac_artefact_versions,
        "finalMonthlyCostAud": report.final_monthly_cost_aud,
        "generatedAt": report.generated_at.isoformat(),
        "accessibilityConformance": report.accessibility_conformance,
    }
