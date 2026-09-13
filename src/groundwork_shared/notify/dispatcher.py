"""Deployment-outcome email notification (T096; FR-051, FR-004c, FR-004e).

FR-051 requires notifying the customer on completion or failure, with no secrets or credentials in
the notification body. This module builds that notification and sends it — nothing more.

**Why `groundwork_shared`, not `groundwork_controlplane.notify` as the original task plan named
it.** The only place this codebase learns a deployment reached a terminal state is
``Sequencer._mark_succeeded``/``_mark_halted`` (``groundwork_orchestrator.engine.sequencer``) — the
same place report generation and archival (T092/T093) are triggered, and for the same reason:
The deterministic-execution boundary forbids the orchestrator from importing
``groundwork_controlplane`` (``tests/unit/test_import_boundaries.py``), so a module that must be
*called from* the
orchestrator's own completion path cannot live in the control plane. Azure Communication Services
Email authenticates with the same first-party workload identity every other Azure client in this
process already uses (``DefaultAzureCredential`` — live-verified against Microsoft Learn 2026-08-02:
``EmailClient(endpoint, DefaultAzureCredential())``, no connection string) — no control-plane-shaped
coupling, the same reasoning that already moved cost estimation out of
``groundwork_controlplane.costing`` for T075a.

**Recipient resolution.** ``NotificationDispatcher.notify_deployment_outcome`` takes the resolved
recipient as a parameter; resolution is the caller's responsibility. In production the caller is
``worker.py``'s ``_TenantNotifier``, which reads ``CustomerTenant.notification_email`` — the
email address gathered and explicitly reconfirmed from the customer during conversation
(``ClarificationTracker.NOTIFICATION_EMAIL`` / FR-002, FR-004b). If no address has been recorded,
the notification is skipped with a warning rather than delivered to an unknown address.

**Teams delivery is not attempted.** It depends on the same M365 Agents SDK /
app-registration decision blocking T052 (the project's internal implementation notes, not
included in this release, own T052 entry) — building a Teams sender here would duplicate an
architecture decision that task is already waiting on.
"""

from __future__ import annotations

import html as _html
from dataclasses import dataclass
from typing import Any, Protocol

from groundwork_contracts.audit import DeploymentReport, ReportOutcome, StageSummary
from groundwork_shared.telemetry.scrubbing import scrub_text

_OUTCOME_LABEL: dict[ReportOutcome, str] = {
    ReportOutcome.SUCCEEDED: "completed successfully",
    ReportOutcome.HALTED: "did not complete and requires attention",
    ReportOutcome.ROLLED_BACK: "was rolled back",
}

_OWNERSHIP_NOTICE = (
    "Going forward, further Fabric changes run through your own Azure DevOps pipeline — "
    + "Groundwork now only monitors for configuration drift."
)

_HALTED_RECOVERY_NOTICE = (
    "Your infrastructure built so far is preserved in your subscription — nothing is deleted "
    "automatically. You have three recovery options: retry (resume from the last completed "
    "stage), forward-fix (resume the same way, once you've corrected the underlying issue), or "
    "rollback (redeploy to the last known-good configuration through your own Azure DevOps "
    "pipeline — this requires a separate, distinct approval). Choose one from the app, or ask "
    "your Groundwork contact."
)


def _stage_status_text(stage: StageSummary) -> str:
    if stage.never_ran:
        return "never ran"
    return stage.status.value if stage.status else "unknown"


class NotificationDispatchError(Exception):
    """The email provider rejected or failed to complete a send."""


class EmailSenderLike(Protocol):
    """What :class:`NotificationDispatcher` depends on — injectable so a test can supply a fake
    with no ACS Email resource behind it, the same seam every other Azure-writing module in this
    session uses (``ReportArchiveStoreLike``, ``WhatIfCaptureLike``)."""

    async def send(
        self,
        *,
        to_address: str,
        to_display_name: str,
        subject: str,
        plain_text: str,
        html: str,
    ) -> str:
        """Send one message; return the provider's operation id."""
        ...


@dataclass(frozen=True, slots=True)
class NotificationContent:
    subject: str
    plain_text: str
    html: str


def build_notification_content(report: DeploymentReport) -> NotificationContent:
    """Pure formatting: a :class:`DeploymentReport` becomes an outcome-labelled subject/body. No
    I/O, so this is independently testable without a fake email provider at all.

    Every field read from ``DeploymentReport`` is already schema-validated, structured data — no
    stage carries a free-text error message (``StageSummary`` has ``stage_name``/``status``/
    ``never_ran``/``attempts`` only, nothing an Azure SDK exception could have written into). The
    ``scrub_text`` call below is a second, defensive control, not the primary one — SC-013's primary
    control is that the report contract itself has nowhere for a secret to hide.

    FR-004e: status is conveyed only through the ``status``/``neverRan`` text itself, never colour,
    and the HTML body uses real table semantics (``<caption>``, ``scope="col"``/``scope="row"``) so
    the summary is screen-reader navigable — the same requirement ``report_render.py`` (T094) will
    apply to the standalone report.
    """
    label = _OUTCOME_LABEL[report.outcome]
    lines = [f"Your Groundwork deployment {report.deployment_id} {label}.", "", "Stage summary:"]
    for stage in report.stage_summary:
        status_text = _stage_status_text(stage)
        lines.append(f"  - {stage.stage_name}: {status_text} ({stage.attempts} attempt(s))")
    lines.append("")
    if report.outcome is ReportOutcome.SUCCEEDED:
        lines.append(_OWNERSHIP_NOTICE)
        lines.append("")
    if report.outcome is ReportOutcome.HALTED:
        lines.append(_HALTED_RECOVERY_NOTICE)
        lines.append("")
    if report.resources_created:
        lines.append("Resources created:")
        lines.extend(f"  - {name}" for name in report.resources_created)
        lines.append("")
    lines.append(f"Final estimated monthly cost: AUD {report.final_monthly_cost_aud:.2f}")
    lines.append(f"Full report: {report.blob_uri}")
    plain_text = scrub_text("\n".join(lines), redact_guids=False)

    row_html = "".join(
        "<tr>"
        f'<th scope="row">{_html.escape(stage.stage_name)}</th>'
        f"<td>{_html.escape(_stage_status_text(stage))}</td>"
        f"<td>{stage.attempts}</td>"
        "</tr>"
        for stage in report.stage_summary
    )
    ownership_html = ""
    if report.outcome is ReportOutcome.SUCCEEDED:
        ownership_html = f"<p>{_html.escape(_OWNERSHIP_NOTICE)}</p>"
    if report.outcome is ReportOutcome.HALTED:
        ownership_html = f"<p>{_html.escape(_HALTED_RECOVERY_NOTICE)}</p>"
    body_html = (
        f"<h1>Deployment {_html.escape(report.deployment_id)} {_html.escape(label)}</h1>"
        "<table>"
        "<caption>Stage summary</caption>"
        '<thead><tr><th scope="col">Stage</th><th scope="col">Status</th>'
        '<th scope="col">Attempts</th></tr></thead>'
        f"<tbody>{row_html}</tbody>"
        "</table>"
        f"{ownership_html}"
        f"<p>Final estimated monthly cost: AUD {report.final_monthly_cost_aud:.2f}</p>"
        f'<p><a href="{_html.escape(report.blob_uri)}">Full report</a></p>'
    )
    html_body = scrub_text(body_html, redact_guids=False)

    subject = f"Groundwork deployment {report.deployment_id} {label}"
    return NotificationContent(subject=subject, plain_text=plain_text, html=html_body)


def build_drift_notification_content(
    *, tenant_id: str, subscription_id: str, region: str, blocking_failed_count: int
) -> NotificationContent:
    """Pure formatting for a blocking-drift alert — the customer-journey-map.md Near-Term
    improvement, 2026-09-13: `engine/drift_watch.py` already detects drift on a schedule but had
    no code path to the customer, only to a repository and internal metrics. Reuses the same
    email channel FR-051 already built for deployment outcomes; no new channel needed.

    Deliberately terse: this is an alert to check the app, not a diagnosis. The specific failing
    assertions live in the pull-based readiness report (`GET
    /v1/tenants/{tenantId}/onboarding/readiness-report`); repeating them here would drift out of
    sync with that report's own, more detailed rendering.
    """
    plural = "check" if blocking_failed_count == 1 else "checks"
    lines = [
        f"Groundwork detected configuration drift in subscription {subscription_id} "
        f"(region {region}).",
        "",
        f"{blocking_failed_count} readiness {plural} that previously passed "
        f"{'is' if blocking_failed_count == 1 else 'are'} now failing.",
        "",
        "This does not block anything automatically — it's a signal to review before your next "
        "deployment. See the full readiness report in the app for exactly what changed.",
    ]
    plain_text = scrub_text("\n".join(lines), redact_guids=False)
    body_html = (
        f"<h1>Configuration drift detected</h1>"
        f"<p>Subscription {_html.escape(subscription_id)} (region {_html.escape(region)}).</p>"
        f"<p>{blocking_failed_count} readiness {plural} that previously passed "
        f"{'is' if blocking_failed_count == 1 else 'are'} now failing.</p>"
        "<p>This does not block anything automatically — it's a signal to review before your "
        "next deployment. See the full readiness report in the app for exactly what changed.</p>"
    )
    html_body = scrub_text(body_html, redact_guids=False)
    subject = f"Groundwork: configuration drift detected in {tenant_id}"
    return NotificationContent(subject=subject, plain_text=plain_text, html=html_body)


class NotificationDispatcher:
    """Formats and sends one deployment-outcome or drift-alert email. Nothing here resolves a
    recipient — see the module docstring's "Disclosed, not built" section."""

    def __init__(self, *, email_sender: EmailSenderLike) -> None:
        self._email_sender = email_sender

    async def notify_deployment_outcome(
        self,
        report: DeploymentReport,
        *,
        recipient_email: str,
        recipient_display_name: str,
    ) -> str:
        content = build_notification_content(report)
        return await self._email_sender.send(
            to_address=recipient_email,
            to_display_name=recipient_display_name,
            subject=content.subject,
            plain_text=content.plain_text,
            html=content.html,
        )

    async def notify_drift_detected(
        self,
        *,
        tenant_id: str,
        subscription_id: str,
        region: str,
        blocking_failed_count: int,
        recipient_email: str,
        recipient_display_name: str,
    ) -> str:
        content = build_drift_notification_content(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            region=region,
            blocking_failed_count=blocking_failed_count,
        )
        return await self._email_sender.send(
            to_address=recipient_email,
            to_display_name=recipient_display_name,
            subject=content.subject,
            plain_text=content.plain_text,
            html=content.html,
        )


class _AzureCommunicationEmailSender:
    """Wraps ``azure.communication.email.aio.EmailClient``. The only place that SDK is imported."""

    def __init__(self, client: Any, *, sender_address: str) -> None:
        self._client = client
        self._sender_address = sender_address

    async def send(
        self,
        *,
        to_address: str,
        to_display_name: str,
        subject: str,
        plain_text: str,
        html: str,
    ) -> str:
        poller = await self._client.begin_send(
            {
                "content": {"subject": subject, "plainText": plain_text, "html": html},
                "recipients": {"to": [{"address": to_address, "displayName": to_display_name}]},
                "senderAddress": self._sender_address,
            }
        )
        result = await poller.result()
        if result["status"] != "Succeeded":
            raise NotificationDispatchError(str(result.get("error")))
        return str(result["id"])


def build_email_sender(
    *, acs_endpoint: str, sender_address: str, credential: Any
) -> EmailSenderLike:
    """Construct a real sender against Azure Communication Services Email.

    Entra ID auth via ``DefaultAzureCredential``/workload identity — live-verified against
    Microsoft Learn 2026-08-02 (``EmailClient(endpoint, DefaultAzureCredential())``), no connection
    string, matching this codebase's secretless discipline. Lazy import, the same
    pattern ``report_archive.py``'s ``build_report_archive_store`` and ``worker.py``'s other
    ``build_*`` factories already use, so importing this module never requires the SDK to be
    installed unless a caller actually constructs a real sender.
    """
    from azure.communication.email.aio import EmailClient

    client = EmailClient(acs_endpoint, credential)
    return _AzureCommunicationEmailSender(client, sender_address=sender_address)
