"""``_TenantNotifier`` (``worker.py``) — the concrete adapter wired into both the deployment-
outcome notifier seam (``Sequencer.NotifierLike``) and the drift-alert seam
(``drift_watch.DriftNotifierLike``).

No test file existed for this class before; this one is deliberately narrow — it covers the
``notify_drift_detected`` pass-through added 2026-09-13 (customer-journey-map.md Near-Term
improvement), not a full ``lifespan()`` replication. ``drift_watch.py``'s own unit tests already
prove the *caller* side (drift_watch invoking ``notifier.notify_drift_detected(...)`` with the
right arguments against a fake); this proves the *adapter* side, end to end against a real
``NotificationDispatcher`` (only the email transport is faked, the same seam
``test_notify_dispatcher.py`` uses). The one fact this file cannot verify — that ``worker.py``'s
lifespan actually passes the same ``notifier`` object into ``run_drift_watch_forever(...)`` — is a
one-line, directly-readable call site (``notifier=notifier``), not a code path prone to a silent
logic regression; a full lifespan test to cover it would need to fake most of the orchestrator's
external dependencies (Cosmos, Storage, Foundry, every Stage) for a single kwarg.
"""

from __future__ import annotations

from typing import Any

from groundwork_contracts.tenant import CustomerTenant
from groundwork_orchestrator.worker import _TenantNotifier
from groundwork_shared.notify.dispatcher import NotificationDispatcher


class _FakeTenantRepo:
    """Unused by notify_drift_detected — present only because _TenantNotifier.__init__
    requires it, matching notify_deployment_outcome's own repo-lookup shape."""

    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None:
        raise AssertionError("notify_drift_detected must not look up the tenant repository")


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


async def test_notify_drift_detected_forwards_to_the_real_dispatcher() -> None:
    sender = _FakeEmailSender()
    notifier = _TenantNotifier(
        tenant_repo=_FakeTenantRepo(), dispatcher=NotificationDispatcher(email_sender=sender)
    )

    await notifier.notify_drift_detected(
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id="33333333-3333-3333-3333-333333333333",
        region="australiaeast",
        blocking_failed_count=2,
        recipient_email="customer@example.invalid",
        recipient_display_name="Test Customer",
    )

    assert len(sender.calls) == 1
    call = sender.calls[0]
    assert call["to_address"] == "customer@example.invalid"
    assert call["to_display_name"] == "Test Customer"
    assert "11111111-1111-1111-1111-111111111111" in call["subject"]
    assert "33333333-3333-3333-3333-333333333333" in call["plain_text"]
    assert "2 readiness checks" in call["plain_text"]
