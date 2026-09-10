"""Accessible HTML rendering for stored readiness assessments.

Mirrors ``groundwork_orchestrator.state.report_render``'s bare, semantic HTML approach for a
customer-forwardable readiness report: one ``<h1>``, descriptive ``<h2>`` sections, genuine table
semantics, and explicit status text rather than colour-dependent meaning.
"""

from __future__ import annotations

import html as _html
from datetime import datetime

from groundwork_contracts.readiness import ReadinessSummary, ValidationResult
from groundwork_shared.telemetry.scrubbing import scrub_text


def _safe_dynamic(value: str) -> str:
    return _html.escape(scrub_text(value, redact_guids=False))


def _deployability_text(summary: ReadinessSummary) -> str:
    return "Ready for deployment" if summary.deployable else "Not ready for deployment"


def _result_rows_html(results: tuple[ValidationResult, ...]) -> str:
    return "".join(
        "<tr>"
        f'<th scope="row">{_safe_dynamic(result.assertion_id)}</th>'
        f"<td>{_safe_dynamic(result.design_area.value)}</td>"
        f"<td>{_safe_dynamic(result.severity.value)}</td>"
        f"<td>{_safe_dynamic(result.status.value)}</td>"
        f"<td>{_safe_dynamic(result.finding)}</td>"
        f"<td>{_safe_dynamic(result.remediation or 'Not required.')}</td>"
        "</tr>"
        for result in results
    )


def render_readiness_html(
    *,
    tenant_id: str,
    subscription_id: str,
    summary: ReadinessSummary,
    generated_at: datetime,
) -> str:
    """Render a stored readiness assessment as a self-contained HTML document."""
    title = "Groundwork readiness assessment report"
    blocking_count = len(summary.blocking_failures)
    unreachable_count = len(summary.unreachable)
    counts = summary.counts()
    return (
        "<!doctype html>"
        '<html lang="en-AU"><head><meta charset="utf-8">'
        f"<title>{title}</title></head>"
        "<body>"
        f"<h1>{title}</h1>"
        "<h2>Assessment overview</h2>"
        "<dl>"
        f"<dt>Tenant ID</dt><dd>{_safe_dynamic(tenant_id)}</dd>"
        f"<dt>Subscription ID</dt><dd>{_safe_dynamic(subscription_id)}</dd>"
        f"<dt>Readiness</dt><dd>{_deployability_text(summary)}</dd>"
        f"<dt>Contract version</dt><dd>{_safe_dynamic(summary.contract_version)}</dd>"
        f"<dt>Assessment evaluated</dt><dd>{_safe_dynamic(summary.evaluated_at.isoformat())}</dd>"
        f"<dt>Report generated</dt><dd>{_safe_dynamic(generated_at.isoformat())}</dd>"
        "</dl>"
        "<h2>Outcome summary</h2>"
        "<dl>"
        f"<dt>Blocking findings</dt><dd>{blocking_count}</dd>"
        f"<dt>Unreachable checks</dt><dd>{unreachable_count}</dd>"
        f"<dt>Passed checks</dt><dd>{counts['passed']}</dd>"
        f"<dt>Failed checks</dt><dd>{counts['failed']}</dd>"
        f"<dt>Unreachable results</dt><dd>{counts['unreachable']}</dd>"
        "</dl>"
        "<h2>Readiness results</h2>"
        "<table>"
        "<caption>Latest stored readiness result for every assessed landing zone check</caption>"
        "<thead><tr>"
        '<th scope="col">Assertion</th>'
        '<th scope="col">Design area</th>'
        '<th scope="col">Severity</th>'
        '<th scope="col">Status</th>'
        '<th scope="col">Finding</th>'
        '<th scope="col">Remediation</th>'
        "</tr></thead>"
        f"<tbody>{_result_rows_html(summary.results)}</tbody>"
        "</table>"
        "</body></html>"
    )
