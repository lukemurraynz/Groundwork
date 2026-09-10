"""T094 — accessible report rendering (FR-004c, FR-004e, WCAG 2.2 AA)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_orchestrator.state.report_render import render_report_html

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64


def _report(
    *,
    outcome: ReportOutcome = ReportOutcome.HALTED,
    resources_created: tuple[str, ...] = ("proj-groundwork-33333333",),
    iac_artefact_versions: dict[str, str] | None = None,
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
        resources_created=resources_created,
        iac_artefact_versions=(
            {"standard-production-fabric": "1.0.0"}
            if iac_artefact_versions is None
            else iac_artefact_versions
        ),
        final_monthly_cost_aud=412.50,
        blob_uri="https://example.invalid/reports/dddddddd.json",
        content_hash="sha256:" + "0" * 64,
        generated_at=NOW,
        retention_expires_at=NOW + timedelta(days=365),
    )


def test_render_produces_a_heading_hierarchy() -> None:
    document = render_report_html(_report())

    assert "<h1>" in document
    assert document.count("<h2>") >= 4


def test_render_uses_real_table_semantics_for_stage_summary() -> None:
    document = render_report_html(_report())

    assert "<caption>" in document
    assert 'scope="col"' in document
    assert 'scope="row"' in document
    assert "devops_project" in document
    assert "infrastructure" in document


def test_render_states_every_stage_status_including_never_ran() -> None:
    document = render_report_html(_report())

    assert "succeeded" in document
    assert "failed" in document
    assert "never ran" in document


def test_render_never_conveys_status_through_colour_alone() -> None:
    document = render_report_html(_report())

    assert "color:" not in document.lower()
    assert "background" not in document.lower()
    assert "<style" not in document.lower()


def test_render_uses_list_semantics_for_resources_and_iac_versions() -> None:
    document = render_report_html(_report())

    assert "<ul>" in document
    assert "<li>proj-groundwork-33333333</li>" in document
    assert "<dl>" in document
    assert "<dt>standard-production-fabric</dt>" in document


def test_render_handles_no_resources_and_no_iac_versions() -> None:
    document = render_report_html(_report(resources_created=(), iac_artefact_versions={}))

    assert "No resources were created" in document
    assert "No IaC artefact versions recorded" in document


def test_render_escapes_html_special_characters_in_resource_names() -> None:
    document = render_report_html(_report(resources_created=("<script>alert(1)</script>",)))

    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;" in document


def test_render_includes_deployment_id_and_authority_chain() -> None:
    document = render_report_html(_report())

    assert DEPLOYMENT_ID in document
    assert PLAN_HASH in document
    assert APPROVAL_ID in document
