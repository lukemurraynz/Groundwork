"""Accessible report rendering (T094; FR-004c, FR-004e, WCAG 2.2 AA).

``DeploymentReport.accessibility_conformance`` asserts ``"WCAG-2.2-AA"`` on every report
(``groundwork_contracts.audit``) — this module is what makes that claim true. Nothing before this
task actually rendered a report as anything but raw JSON; ``api/reports.py``'s
``GET /deployments/{deploymentId}/report`` now content-negotiates and calls
:func:`render_report_html` for an ``Accept: text/html`` request (see that module).

What WCAG 2.2 AA requires that this rendering makes concrete, not just asserted:

- **1.3.1 Info and Relationships**: a real ``<table>`` with ``<caption>`` and ``scope="col"``/
  ``scope="row"`` headers for the stage summary, not a div grid; a genuine heading hierarchy
  (``<h1>`` for the report title, ``<h2>`` per section) a screen reader's heading navigation can
  traverse.
- **1.4.1 Use of Color**: every stage's status is stated as text (``succeeded``/``failed``/
  ``never ran``), never conveyed by colour, background, or an icon alone (FR-004e).
- **2.4.6 Headings and Labels**: section headings describe their content (``"Stage summary"``,
  ``"Resources created"``), never a generic ``"Details"``.
- **4.1.2 Name, Role, Value**: the resource list and IaC artefact versions use real ``<ul>``/
  ``<dl>`` semantics, not ``<br>``-separated text a screen reader collapses into one run-on string.

Pure function, no I/O — independently testable, and reusable by any future presentation surface (a
portal page, a Teams adaptive card) without re-deriving the markup.
"""

from __future__ import annotations

import html as _html

from groundwork_contracts.audit import AuthorityChain, DeploymentReport, StageSummary
from groundwork_shared.telemetry.scrubbing import scrub_text


def _stage_status_text(stage: StageSummary) -> str:
    if stage.never_ran:
        return "never ran"
    return stage.status.value if stage.status else "unknown"


def _stage_rows_html(stage_summary: tuple[StageSummary, ...]) -> str:
    return "".join(
        "<tr>"
        f'<th scope="row">{_html.escape(stage.stage_name)}</th>'
        f"<td>{_html.escape(_stage_status_text(stage))}</td>"
        f"<td>{stage.attempts}</td>"
        "</tr>"
        for stage in stage_summary
    )


def _authority_html(authority: AuthorityChain) -> str:
    return (
        "<dl>"
        f"<dt>Approved plan</dt><dd>{_html.escape(authority.plan_hash)}</dd>"
        f"<dt>Approval</dt><dd>{_html.escape(authority.approval_id)}</dd>"
        "</dl>"
    )


def _resources_html(resources_created: tuple[str, ...]) -> str:
    if not resources_created:
        return "<p>No resources were created.</p>"
    items = "".join(
        f"<li>{_html.escape(scrub_text(name, redact_guids=False))}</li>"
        for name in resources_created
    )
    return f"<ul>{items}</ul>"


def _iac_versions_html(iac_artefact_versions: dict[str, str]) -> str:
    if not iac_artefact_versions:
        return "<p>No IaC artefact versions recorded.</p>"
    entries = "".join(
        f"<dt>{_html.escape(scrub_text(name, redact_guids=False))}</dt>"
        f"<dd>{_html.escape(scrub_text(version, redact_guids=False))}</dd>"
        for name, version in iac_artefact_versions.items()
    )
    return f"<dl>{entries}</dl>"


def render_report_html(report: DeploymentReport) -> str:
    """Render one deployment report as a self-contained, WCAG 2.2 AA accessible HTML document."""
    title = f"Groundwork deployment {report.deployment_id} report"
    return (
        "<!doctype html>"
        '<html lang="en-AU"><head><meta charset="utf-8">'
        f"<title>{_html.escape(title)}</title></head>"
        "<body>"
        f"<h1>Deployment {_html.escape(report.deployment_id)} "
        f"— {_html.escape(report.outcome.value)}</h1>"
        f"<h2>Authority</h2>{_authority_html(report.authority)}"
        "<h2>Stage summary</h2>"
        "<table>"
        "<caption>Outcome of every stage this deployment's blueprint declares</caption>"
        '<thead><tr><th scope="col">Stage</th><th scope="col">Status</th>'
        '<th scope="col">Attempts</th></tr></thead>'
        f"<tbody>{_stage_rows_html(report.stage_summary)}</tbody>"
        "</table>"
        f"<h2>Resources created</h2>{_resources_html(report.resources_created)}"
        f"<h2>IaC artefact versions</h2>{_iac_versions_html(report.iac_artefact_versions)}"
        "<h2>Cost</h2>"
        f"<p>Final estimated monthly cost: AUD {report.final_monthly_cost_aud:.2f}</p>"
        "</body></html>"
    )
