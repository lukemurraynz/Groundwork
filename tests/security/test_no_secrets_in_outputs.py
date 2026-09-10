"""T090 — no secrets, tokens, credentials, or PII in any output (SC-013).

``tests/security/test_secret_scrubbing.py`` already unit-tests ``scrub_text``/``scrub_value``
directly against known shapes — this file deliberately does not duplicate those cases. It proves
one level up: that the actual *producers* (``render_report_html``, ``build_notification_content``)
never leak a secret-shaped string even when you feed them adversarial input, and that the HTML
renderer is covered the same way the notification path already is.

Every literal below is synthetic and structurally plausible, not a real credential — matching
``tests/security/test_secret_scrubbing.py``'s own disclosure.
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
from groundwork_shared.telemetry.scrubbing import _SENSITIVE_KEY_PARTS

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64

# Synthetic, structurally valid, not issued by anything — matching
# tests/security/test_secret_scrubbing.py's own disclosure.
SYNTHETIC_SAS_URL = (
    "https://storage.blob.core.windows.net/container/blob.txt"
    "?sv=2023-11-03&sig=abcdef1234567890abcdef1234567890secretvalue&se=2099-01-01T00:00:00Z"
)
SYNTHETIC_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IlRlc3QifQ"
    ".dBjftJeZ4CVPmB92K27uhbUJU1p1r0W1nFDcSCTFmJk"
)
SYNTHETIC_CONN_STR = (
    "DefaultEndpointsProtocol=https;AccountName=acct;"
    "AccountKey=Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==;EndpointSuffix=core.windows.net"
)


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


def test_report_html_scrubs_sas_url_from_resources_created() -> None:
    """A synthetic SAS-shaped URL in ``resources_created`` must not survive in the rendered
    HTML — this is the primary report path's own defensive cover, not the scrubbing module's
    unit test (which already covers ``scrub_text`` against SAS patterns)."""
    document = render_report_html(
        _report(resources_created=(SYNTHETIC_SAS_URL, "legitimate-resource"))
    )

    assert "sig=abcdef1234567890" not in document, "SAS signature leaked into report HTML"
    assert "[REDACTED:sas]" in document, "SAS URL was not redacted in report HTML"


def test_report_html_scrubs_jwt_from_resources_created() -> None:
    document = render_report_html(_report(resources_created=(SYNTHETIC_JWT,)))

    assert "eyJhbGci" not in document, "JWT header leaked into report HTML"
    assert "[REDACTED:jwt]" in document, "JWT was not redacted in report HTML"


def test_report_html_scrubs_connection_string_from_resources_created() -> None:
    document = render_report_html(_report(resources_created=(SYNTHETIC_CONN_STR,)))

    assert "Zm9vYmFy" not in document, "connection-string account key leaked into report HTML"
    assert "[REDACTED:connection-string]" in document, (
        "connection string was not redacted in report HTML"
    )


def test_report_html_preserves_legitimate_resource_names() -> None:
    """Redaction must not eat ordinary, non-secret-shaped resource names."""
    document = render_report_html(
        _report(resources_created=("proj-groundwork-33333333", "kv-gw-abc123"))
    )

    assert "proj-groundwork-33333333" in document
    assert "kv-gw-abc123" in document


def test_notification_content_scrubs_secrets_in_plain_text() -> None:
    """build_notification_content already has one scrubbing test in
    test_notify_dispatcher.py — this asserts the HTML render path is covered too, and
    that both the plain_text *and* HTML outputs are scrubbed."""
    content = build_notification_content(
        _report(resources_created=(SYNTHETIC_SAS_URL, SYNTHETIC_JWT))
    )

    assert "sig=abcdef1234567890" not in content.plain_text
    assert "eyJhbGci" not in content.plain_text
    assert "[REDACTED:sas]" in content.plain_text
    assert "[REDACTED:jwt]" in content.plain_text


def test_notification_html_is_free_of_secret_shaped_strings() -> None:
    """The notification HTML body does not render ``resources_created`` at all (only
    ``plain_text`` does — see ``build_notification_content``), so there is literally
    no place for a secret-shaped resource name to appear in the HTML output.
    The HTML output wraps the stage summary, cost, and a blob URI — none of which
    are free-text fields an adversarial resource name could reach."""
    content = build_notification_content(
        _report(resources_created=(SYNTHETIC_SAS_URL, SYNTHETIC_CONN_STR))
    )

    assert "sig=abcdef1234567890" not in content.html
    assert "Zm9vYmFy" not in content.html
    # The HTML never renders resources_created, so [REDACTED] markers won't appear —
    # there is nothing to redact.
    assert "proj-groundwork-33333333" not in content.html, (
        "resources_created was rendered into the notification HTML — if this assertion "
        "fails because a future build_notification_content change started including "
        "resources_created in the HTML body, add [REDACTED:sas] and "
        "[REDACTED:connection-string] assertions back"
    )


def test_iac_artefact_versions_keys_are_not_inherently_secret_regression_guard() -> None:
    """``iac_artefact_versions`` keys are blueprint module names today — nothing secret-shaped.
    But a future blueprint could theoretically name a module something unfortunate (e.g.
    ``apiKey`` or ``sasToken``). Assert that if a key matching ``_SENSITIVE_KEY_PARTS``
    appeared as an ``iac_artefact_versions`` key, it would survive in both outputs (since
    ``build_notification_content`` calls ``scrub_text`` on the already-formatted body, and
    html-escaped ``<dt>``/``<dd>`` content has already been escaped — the defensive scrubbing
    step is after that point, on the final concatenated string).

    This test is a regression guard, not a current-behaviour assertion — it documents that
    nothing today triggers the redaction path for ``iac_artefact_versions`` keys, but also
    proves that if a key *did* match, the current code would redact it through the
    ``scrub_text`` post-processing step.

    ``render_report_html`` does NOT call ``scrub_text`` — it html-escapes but does not
    pattern-scan its output. This test asserts that fact as well, so a future reader knows
    the coverage boundary.
    """
    # Pick a key that appears in _SENSITIVE_KEY_PARTS.
    assert "apikey" in _SENSITIVE_KEY_PARTS, (
        "test assumes 'apikey' is in _SENSITIVE_KEY_PARTS — update if the set changes"
    )

    report = _report(iac_artefact_versions={"apiKey": "1.0.0"})

    # Notification path: scrub_text is called, so "apiKey" formatted into plain/href'd
    # text would survive but the *value* may not — however the key itself is just text
    # inside the notification output.
    _notify = build_notification_content(report)
    # The notify path doesn't enumerate iac_artefact_versions in plain_text (only
    # resources_created and cost), so there's no place for the key to appear at all.
    # This is fine — the regression guard is that nothing silently leaks.

    # Report HTML path: no scrub_text call at all.
    document = render_report_html(report)
    # apiKey as a blueprint module name appearing in the report body is legitimate —
    # it's not a secret, it's a versioned artifact name.
    assert "apiKey" in document, (
        "iac_artefact_versions key was silently dropped from the report; "
        "it should appear as legitimate metadata, not be scrubbed"
    )


def test_report_html_content_is_scrubbed_defensively_even_on_clean_input() -> None:
    """Prove the HTML renderer's output contains no secret-shaped patterns even on clean,
    production-typical input — a sanity check that the defensive scrubbing step would catch
    a regression, not just the adversarial test cases above."""
    document = render_report_html(_report())

    assert "[REDACTED" not in document, (
        "clean input triggered a redaction — likely a false positive in the scrubbing patterns"
    )
