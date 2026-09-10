"""T096 — deployment-outcome email notification (FR-051, FR-004c, FR-004e, SC-013)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_shared.notify.dispatcher import (
    NotificationDispatcher,
    build_notification_content,
)

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
APPROVAL_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
PLAN_HASH = "sha256:" + "a" * 64


_HALTED_STAGE_SUMMARY = (
    StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
    StageSummary(stage_name="infrastructure", status=StageStatus.FAILED, attempts=3),
    StageSummary(stage_name="identity", never_ran=True, attempts=0),
)
_SUCCEEDED_STAGE_SUMMARY = (
    StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
    StageSummary(stage_name="infrastructure", status=StageStatus.SUCCEEDED, attempts=1),
)


def _report(
    *,
    outcome: ReportOutcome = ReportOutcome.HALTED,
    resources_created: tuple[str, ...] = ("proj-groundwork-33333333",),
    stage_summary: tuple[StageSummary, ...] | None = None,
) -> DeploymentReport:
    return DeploymentReport(
        report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        deployment_id=DEPLOYMENT_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        outcome=outcome,
        stage_summary=stage_summary or _HALTED_STAGE_SUMMARY,
        resources_created=resources_created,
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=412.50,
        blob_uri="https://example.invalid/reports/dddddddd.json",
        content_hash="sha256:" + "0" * 64,
        generated_at=NOW,
        retention_expires_at=NOW + timedelta(days=365),
    )


def test_content_names_outcome_and_deployment() -> None:
    content = build_notification_content(_report(outcome=ReportOutcome.HALTED))

    assert DEPLOYMENT_ID in content.subject
    assert "did not complete" in content.subject
    assert DEPLOYMENT_ID in content.plain_text


def test_content_restates_pipeline_ownership_only_on_success() -> None:
    succeeded = build_notification_content(
        _report(outcome=ReportOutcome.SUCCEEDED, stage_summary=_SUCCEEDED_STAGE_SUMMARY)
    )
    halted = build_notification_content(_report(outcome=ReportOutcome.HALTED))

    assert "own Azure DevOps pipeline" in succeeded.plain_text
    assert "own Azure DevOps pipeline" in succeeded.html
    assert "own Azure DevOps pipeline" not in halted.plain_text
    assert "own Azure DevOps pipeline" not in halted.html


def test_content_lists_every_stage_including_never_ran() -> None:
    content = build_notification_content(_report())

    assert "devops_project: succeeded" in content.plain_text
    assert "infrastructure: failed" in content.plain_text
    assert "identity: never ran" in content.plain_text


def test_content_never_conveys_status_by_colour_alone() -> None:
    """FR-004e: no inline colour styling anywhere in the HTML body."""
    content = build_notification_content(_report())

    assert "color:" not in content.html.lower()
    assert "background" not in content.html.lower()


def test_content_html_uses_table_semantics() -> None:
    """FR-004e: screen-reader navigable — a real <table> with a caption and header scopes."""
    content = build_notification_content(_report())

    assert "<caption>" in content.html
    assert 'scope="col"' in content.html
    assert 'scope="row"' in content.html
    assert "devops_project" in content.html


def test_content_scrubs_secret_shaped_resource_names() -> None:
    """SC-013: defensive scrub applies even though resources_created is normally deterministic."""
    leaky = "https://storage.blob.core.windows.net/x?sv=2020&sig=abcdef1234567890secretvalue"
    content = build_notification_content(_report(resources_created=(leaky,)))

    assert "sig=abcdef1234567890secretvalue" not in content.plain_text
    assert "sig=abcdef1234567890secretvalue" not in content.html
    assert "[REDACTED:sas]" in content.plain_text


def test_content_preserves_deployment_id_despite_guid_shape() -> None:
    """The recipient must still be able to read their own deployment id in the notification."""
    content = build_notification_content(_report())

    assert DEPLOYMENT_ID in content.plain_text
    assert "[REDACTED:guid]" not in content.plain_text


class _FakeEmailSender:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def send(
        self,
        *,
        to_address: str,
        to_display_name: str,
        subject: str,
        plain_text: str,
        html: str,
    ) -> str:
        self.calls.append(
            {
                "to_address": to_address,
                "to_display_name": to_display_name,
                "subject": subject,
                "plain_text": plain_text,
                "html": html,
            }
        )
        return "op-123"


class _RaisingEmailSender:
    async def send(self, **_: Any) -> str:
        raise RuntimeError("provider unavailable")


async def test_dispatcher_sends_formatted_content_to_the_given_recipient() -> None:
    sender = _FakeEmailSender()
    dispatcher = NotificationDispatcher(email_sender=sender)

    operation_id = await dispatcher.notify_deployment_outcome(
        _report(),
        recipient_email="customer@example.invalid",
        recipient_display_name="Test Customer",
    )

    assert operation_id == "op-123"
    assert len(sender.calls) == 1
    assert sender.calls[0]["to_address"] == "customer@example.invalid"
    assert sender.calls[0]["to_display_name"] == "Test Customer"
    assert DEPLOYMENT_ID in sender.calls[0]["subject"]


async def test_dispatcher_propagates_sender_failures() -> None:
    """The dispatcher does not swallow errors itself — Sequencer._generate_report's own
    try/except around notify_deployment_outcome is where that discipline lives (see its
    docstring), so this class stays a thin, honest wrapper."""
    dispatcher = NotificationDispatcher(email_sender=_RaisingEmailSender())

    with pytest.raises(RuntimeError, match="provider unavailable"):
        await dispatcher.notify_deployment_outcome(
            _report(), recipient_email="customer@example.invalid", recipient_display_name="Test"
        )
