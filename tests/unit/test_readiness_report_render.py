"""Accessible readiness report rendering."""

from __future__ import annotations

from datetime import UTC, datetime

from groundwork_contracts.blueprint import DesignAreaName
from groundwork_contracts.readiness import (
    AssertionSeverity,
    ReadinessSummary,
    ValidationResult,
    ValidationStatus,
)
from groundwork_shared.validation.report import render_readiness_html

NOW = datetime(2026, 8, 26, 10, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
SUBSCRIPTION_ID = "22222222-2222-2222-2222-222222222222"


def _summary(*, finding: str = "Address space overlaps an existing VNet.") -> ReadinessSummary:
    return ReadinessSummary(
        contract_version="1.0.0",
        evaluated_at=NOW,
        results=(
            ValidationResult(
                assertion_id="network.address-space",
                contract_version="1.0.0",
                design_area=DesignAreaName.NETWORK_TOPOLOGY,
                status=ValidationStatus.FAILED,
                severity=AssertionSeverity.BLOCKING,
                finding=finding,
                remediation="Choose a non-overlapping address range.",
                evaluated_at=NOW,
            ),
            ValidationResult(
                assertion_id="identity.breakglass",
                contract_version="1.0.0",
                design_area=DesignAreaName.IDENTITY_AND_ACCESS,
                status=ValidationStatus.PASSED,
                severity=AssertionSeverity.ADVISORY,
                finding="Break-glass account exists and is monitored.",
                evaluated_at=NOW,
            ),
            ValidationResult(
                assertion_id="management.activity-log",
                contract_version="1.0.0",
                design_area=DesignAreaName.MANAGEMENT,
                status=ValidationStatus.UNREACHABLE,
                severity=AssertionSeverity.BLOCKING,
                finding="Azure policy endpoint could not be reached.",
                remediation="Restore network reachability and re-run readiness.",
                evaluated_at=NOW,
            ),
        ),
    )


def test_render_readiness_report_uses_accessible_document_structure() -> None:
    document = render_readiness_html(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        summary=_summary(),
        generated_at=NOW,
    )

    assert '<html lang="en-AU">' in document
    assert document.count("<h1>") == 1
    assert document.count("<h2>") >= 3
    assert "<caption>" in document
    assert 'scope="col"' in document
    assert 'scope="row"' in document


def test_render_readiness_report_states_status_as_text() -> None:
    document = render_readiness_html(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        summary=_summary(),
        generated_at=NOW,
    )

    assert "failed" in document
    assert "passed" in document
    assert "unreachable" in document
    assert "color:" not in document.lower()
    assert "background" not in document.lower()
    assert "<style" not in document.lower()


def test_render_readiness_report_escapes_html_and_scrubs_dynamic_strings() -> None:
    document = render_readiness_html(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        summary=_summary(
            finding=(
                "<script>alert(1)</script> Bearer eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOiIxIn0.signature"
            )
        ),
        generated_at=NOW,
    )

    assert "<script>alert(1)</script>" not in document
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in document
    assert "eyJhbGciOiJIUzI1NiJ9" not in document
    assert "[REDACTED:bearer]" in document or "[REDACTED:jwt]" in document


def test_render_readiness_report_includes_identifiers_and_summary_counts() -> None:
    document = render_readiness_html(
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        summary=_summary(),
        generated_at=NOW,
    )

    assert TENANT_ID in document
    assert SUBSCRIPTION_ID in document
    assert "Not ready for deployment" in document
    assert "Blocking findings</dt><dd>2</dd>" in document
    assert "Passed checks</dt><dd>1</dd>" in document
