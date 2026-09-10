"""_TenantNotifier — resolves email recipients from CustomerTenant for deployment notifications."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from groundwork_contracts.audit import (
    AuthorityChain,
    DeploymentReport,
    ReportOutcome,
    StageStatus,
    StageSummary,
)
from groundwork_contracts.tenant import CustomerTenant

# Private class under test — imported directly as it is the adapter being tested.
from groundwork_orchestrator.worker import _TenantNotifier
from groundwork_shared.notify.dispatcher import NotificationDispatcher

_NOW = datetime(2026, 8, 15, tzinfo=UTC)
_TENANT_ID = "11111111-1111-1111-1111-111111111111"
_DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
_PLAN_HASH = "sha256:" + "a" * 64


def _report() -> DeploymentReport:
    return DeploymentReport(
        report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
        deployment_id=_DEPLOYMENT_ID,
        tenant_id=_TENANT_ID,
        correlation_id="77777777-7777-7777-7777-777777777777",
        authority=AuthorityChain(
            plan_hash=_PLAN_HASH,
            approval_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
        outcome=ReportOutcome.SUCCEEDED,
        stage_summary=(
            StageSummary(stage_name="devops_project", status=StageStatus.SUCCEEDED, attempts=1),
        ),
        resources_created=("proj-groundwork",),
        iac_artefact_versions={"standard-production-fabric": "1.0.0"},
        final_monthly_cost_aud=250.0,
        blob_uri="https://example.invalid/reports/dddddddd.json",
        content_hash="sha256:" + "0" * 64,
        generated_at=_NOW,
        retention_expires_at=_NOW + timedelta(days=365),
    )


@dataclass
class _FakeEmailSender:
    calls: list[dict] = field(default_factory=list)

    async def send(self, *, to_address, to_display_name, subject, plain_text, html) -> str:
        self.calls.append(
            {"to_address": to_address, "to_display_name": to_display_name, "subject": subject}
        )
        return "op-456"


def _tenant(*, notification_email: str | None, contact_display_name: str | None = None):
    return CustomerTenant(
        tenant_id=_TENANT_ID,
        display_name="Contoso",
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        notification_email=notification_email,
        contact_display_name=contact_display_name,
    )


class _FakeTenantRepo:
    def __init__(self, tenant: CustomerTenant | None) -> None:
        self._tenant = tenant

    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None:
        return self._tenant


def _make_notifier(
    tenant: CustomerTenant | None,
) -> tuple[_TenantNotifier, _FakeEmailSender]:
    sender = _FakeEmailSender()
    dispatcher = NotificationDispatcher(email_sender=sender)
    notifier = _TenantNotifier(
        tenant_repo=_FakeTenantRepo(tenant),
        dispatcher=dispatcher,
    )
    return notifier, sender


async def test_sends_to_tenant_notification_email(valid_plan) -> None:
    notifier, sender = _make_notifier(_tenant(notification_email="admin@contoso.onmicrosoft.com"))
    await notifier.notify_deployment_outcome(_report(), valid_plan, tenant_id=_TENANT_ID, now=_NOW)
    assert len(sender.calls) == 1
    assert sender.calls[0]["to_address"] == "admin@contoso.onmicrosoft.com"


async def test_uses_contact_display_name_when_set(valid_plan) -> None:
    notifier, sender = _make_notifier(
        _tenant(notification_email="admin@contoso.onmicrosoft.com", contact_display_name="Jane Doe")
    )
    await notifier.notify_deployment_outcome(_report(), valid_plan, tenant_id=_TENANT_ID, now=_NOW)
    assert sender.calls[0]["to_display_name"] == "Jane Doe"


async def test_falls_back_to_tenant_display_name_when_contact_display_name_absent(
    valid_plan,
) -> None:
    notifier, sender = _make_notifier(
        _tenant(notification_email="admin@contoso.onmicrosoft.com", contact_display_name=None)
    )
    await notifier.notify_deployment_outcome(_report(), valid_plan, tenant_id=_TENANT_ID, now=_NOW)
    assert sender.calls[0]["to_display_name"] == "Contoso"


async def test_skips_notification_when_tenant_has_no_email(valid_plan) -> None:
    notifier, sender = _make_notifier(_tenant(notification_email=None))
    # Must not raise — silently skip.
    await notifier.notify_deployment_outcome(_report(), valid_plan, tenant_id=_TENANT_ID, now=_NOW)
    assert sender.calls == []


async def test_skips_notification_when_tenant_not_found(valid_plan) -> None:
    notifier, sender = _make_notifier(None)
    await notifier.notify_deployment_outcome(_report(), valid_plan, tenant_id=_TENANT_ID, now=_NOW)
    assert sender.calls == []
