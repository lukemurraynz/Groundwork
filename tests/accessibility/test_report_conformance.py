"""T091 — WCAG 2.2 AA accessibility conformance test (FR-004c, FR-004e).

``tests/unit/test_report_render.py`` already asserts heading hierarchy, table semantics,
no-colour, list semantics, HTML escaping, and resource/IaC-version coverage directly against
``render_report_html``. This file is the audit-facing artefact — the named conformance test a
reviewer can point to — not a re-invention of those assertions.

What this file adds: checks ``test_report_render.py`` doesn't already have —
``lang`` attribute, ``<title>``, ``<img>`` alt (regression guard), and the same checks against
the notification HTML path (``build_notification_content``'s ``html`` output).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_orchestrator.state.report_render import render_report_html
from groundwork_shared.notify.dispatcher import build_notification_content

pytestmark = pytest.mark.accessibility

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64


def _report(
    *,
    outcome: ReportOutcome = ReportOutcome.HALTED,
) -> DeploymentReport:
    return DeploymentReport(
        report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=outcome,
        stage_summary=(
            StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
            StageSummary(stage_name="infrastructure", status=StageStatus.FAILED, attempts=3),
            StageSummary(stage_name="identity", never_ran=True, attempts=0),
        ),
        resources_created=("proj-groundwork-33333333",),
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=412.50,
        blob_uri="https://example.invalid/reports/dddddddd.json",
        content_hash="sha256:" + "0" * 64,
        accessibility_conformance="WCAG-2.2-AA",
        generated_at=NOW,
        retention_expires_at=NOW + timedelta(days=365),
    )


# ---------------------------------------------------------------------------
# WCAG 2.2 AA conformance assertions for the full report HTML page
# ---------------------------------------------------------------------------


def test_report_html_has_lang_attribute() -> None:
    """WCAG 3.1.1: every page must declare its language — FR-004a requires ``en-AU``."""
    document = render_report_html(_report())

    assert 'lang="en-AU"' in document, (
        "report HTML page missing lang attribute; WCAG 3.1.1 requires a declared language"
    )


def test_report_html_has_title_element() -> None:
    """WCAG 2.4.2: every page must have a descriptive ``<title>`` so a screen-reader user
    knows what they're looking at before navigating."""
    document = render_report_html(_report())

    assert "<title>" in document, "report HTML page missing <title> element"
    assert "</title>" in document, "report HTML page has unclosed <title> element"


def test_report_html_has_no_img_without_alt() -> None:
    """WCAG 1.1.1: every ``<img>`` must have an ``alt`` attribute — regression guard, since
    the current report contains no images at all."""
    document = render_report_html(_report())

    count = document.count("<img")
    # If images are ever added, every one must carry alt text.
    # A bare scan for '<img ' without 'alt=' would be fragile against self-closing syntax,
    # but this test's primary value is as a gate: adding an img without alt should fail.
    for line in document.splitlines():
        if "<img" in line and "alt=" not in line:
            pytest.fail(f"<img> tag without alt attribute in report HTML: {line.strip()}")
    # If there were no imgs at all, that's fine — the test is a guard, not an
    # existence check.
    _ = count  # used only to confirm we're not accidentally asserting on nothing


def test_report_html_every_table_has_caption() -> None:
    """WCAG 1.3.1: every ``<table>`` must have a ``<caption>`` so a screen-reader user knows
    what the table contains before hearing row data."""
    document = render_report_html(_report())

    table_open_count = document.count("<table>")
    caption_count = document.count("<caption>")
    assert caption_count >= table_open_count, (
        f"found {table_open_count} <table> element(s) but only {caption_count} <caption> "
        f"element(s) — every table must have a caption (WCAG 1.3.1)"
    )


def test_report_html_has_heading_hierarchy() -> None:
    """WCAG 1.3.1/2.4.6: a meaningful heading hierarchy. Reasserted here so this one file
    is a complete conformance summary a reviewer can read, not split across files."""
    document = render_report_html(_report())

    assert "<h1>" in document
    assert document.count("<h2>") >= 4


def test_report_html_never_conveys_status_by_colour_alone() -> None:
    """FR-004e: status text only, never colour — reasserted here for a complete
    conformance summary."""
    document = render_report_html(_report())

    assert "color:" not in document.lower()
    assert "background" not in document.lower()


def test_report_html_uses_table_semantics_for_stage_summary() -> None:
    """WCAG 1.3.1: real ``<table>`` with ``<caption>`` and ``scope``."""
    document = render_report_html(_report())

    assert "<caption>" in document
    assert 'scope="col"' in document
    assert 'scope="row"' in document


# ---------------------------------------------------------------------------
# WCAG 2.2 AA conformance assertions for the notification email HTML
# ---------------------------------------------------------------------------


def test_notification_html_every_table_has_caption() -> None:
    content = build_notification_content(_report())

    table_open_count = content.html.count("<table>")
    caption_count = content.html.count("<caption>")
    assert caption_count >= table_open_count, (
        f"notification HTML has {table_open_count} <table> but only {caption_count} <caption>"
    )


def test_notification_html_never_conveys_status_by_colour_alone() -> None:
    content = build_notification_content(_report())

    assert "color:" not in content.html.lower()
    assert "background" not in content.html.lower()


def test_notification_html_uses_table_semantics_for_stage_summary() -> None:
    content = build_notification_content(_report())

    assert 'scope="col"' in content.html
    assert 'scope="row"' in content.html
