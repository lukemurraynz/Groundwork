"""Periodic drift-watch loop.

Uses the same fake-container pattern as the queue-loop tests: real tenant-scoped repositories over
an in-memory container, fake readiness evaluator, fake tenant-scoped credentials.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from groundwork_contracts.readiness import (
    AssertionSeverity,
    DesignArea,
    DriftSummary,
    DriftVerdict,
    ReadinessSummary,
    ValidationResult,
    ValidationStatus,
)
from groundwork_contracts.tenant import ConsentState, CustomerTenant, SubscriptionEntitlement
from groundwork_orchestrator.engine.drift_watch import (
    DEFAULT_DRIFT_INTERVAL_SECONDS,
    classify_drift,
    drift_watch_enabled,
    poll_once,
)
from groundwork_orchestrator.state.cosmos import (
    TENANT_PARTITION_FIELD,
    TenantRegistry,
    TenantScopedRepository,
)
from groundwork_orchestrator.state.repositories import (
    CustomerTenantRepository,
    DriftSummaryRepository,
)
from groundwork_shared.config.settings import ReadinessSettings
from groundwork_shared.validation.engine import ValidationContext

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
OTHER_SUBSCRIPTION_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 8, 26, 12, 0, 0, tzinfo=UTC)


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[(body[TENANT_PARTITION_FIELD], body["id"])] = dict(body)
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[(body[TENANT_PARTITION_FIELD], body["id"])] = dict(body)
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        document = self.documents.get((partition_key, item))
        if document is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return document

    async def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **_: Any,
    ) -> AsyncIterator[Mapping[str, Any]]:
        for document in self.documents.values():
            if partition_key is not None and document.get(TENANT_PARTITION_FIELD) != partition_key:
                continue
            yield document


class _FakeValueContainer:
    def __init__(self, values: list[str]) -> None:
        self._values = values

    async def query_items(
        self, query: str, *, parameters: list[dict[str, Any]] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        for value in self._values:
            yield value


class _FakeAsyncCredential(AsyncTokenCredential):
    async def get_token(
        self,
        *scopes: str,
        claims: str | None = None,
        tenant_id: str | None = None,
        enable_cae: bool = False,
        **kwargs: Any,
    ) -> AccessToken:
        return AccessToken(token="stub", expires_on=0)  # noqa: S106 - test-only fake token

    async def close(self) -> None:
        return None


class _FakeScopedCredential:
    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id

    def for_tenant(self, tenant_id: str) -> AsyncTokenCredential:
        return _FakeAsyncCredential()


class _FakeCredentialFactory:
    def scoped_to(self, tenant_id: str) -> _FakeScopedCredential:
        return _FakeScopedCredential(tenant_id)


class _SequencedReadinessEngine:
    def __init__(
        self, summaries: dict[tuple[str, str], list[ReadinessSummary] | Exception]
    ) -> None:
        self._summaries = summaries
        self.calls: list[ValidationContext] = []

    async def evaluate(self, context: ValidationContext, *, now: datetime) -> ReadinessSummary:
        self.calls.append(context)
        scripted = self._summaries[(context.tenant_id, context.subscription_id)]
        if isinstance(scripted, Exception):
            raise scripted
        return scripted.pop(0)


def _result(
    *,
    assertion_id: str,
    status: ValidationStatus,
    severity: AssertionSeverity = AssertionSeverity.BLOCKING,
) -> ValidationResult:
    return ValidationResult(
        assertion_id=assertion_id,
        contract_version="1.0.0",
        design_area=DesignArea.NETWORK_TOPOLOGY,
        status=status,
        severity=severity,
        finding=f"{assertion_id}:{status.value}",
        remediation=("fix it" if status.is_blocking_failure else None),
        evaluated_at=NOW,
    )


def _summary(*results: ValidationResult) -> ReadinessSummary:
    return ReadinessSummary(contract_version="1.0.0", results=results, evaluated_at=NOW)


def _tenant(
    tenant_id: str,
    *,
    subscriptions: tuple[SubscriptionEntitlement, ...],
    consent_state: ConsentState = ConsentState.GRANTED,
    notification_email: str | None = None,
) -> CustomerTenant:
    return CustomerTenant(
        tenant_id=tenant_id,
        display_name=f"tenant-{tenant_id[:8]}",
        consent_state=consent_state,
        consent_granted_at=(NOW if consent_state is ConsentState.GRANTED else None),
        subscriptions=subscriptions,
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        devops_organization_url="https://dev.azure.com/customer-org",
        notification_email=notification_email,
    )


class _FakeDriftNotifier:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def notify_drift_detected(
        self,
        *,
        tenant_id: str,
        subscription_id: str,
        region: str,
        blocking_failed_count: int,
        recipient_email: str,
        recipient_display_name: str,
    ) -> None:
        self.calls.append(
            {
                "tenant_id": tenant_id,
                "subscription_id": subscription_id,
                "region": region,
                "blocking_failed_count": blocking_failed_count,
                "recipient_email": recipient_email,
                "recipient_display_name": recipient_display_name,
            }
        )


class _Harness:
    def __init__(self, tenant_ids: list[str]) -> None:
        self.tenant_registry = TenantRegistry(_FakeValueContainer(tenant_ids))
        self.tenant_repository: CustomerTenantRepository = TenantScopedRepository(
            _FakeContainer(), model_cls=CustomerTenant, id_field="tenant_id"
        )
        self.drift_repository: DriftSummaryRepository = TenantScopedRepository(
            _FakeContainer(), model_cls=DriftSummary, id_field="subscription_id"
        )
        self.credential_factory = _FakeCredentialFactory()
        self.readiness_settings = ReadinessSettings(
            orchestrator_principal_id="55555555-5555-5555-5555-555555555555",
            devops_organization_url="https://dev.azure.com/fallback-org",
        )

    async def seed_tenant(self, tenant: CustomerTenant) -> None:
        await self.tenant_repository.create(tenant.tenant_id, tenant)

    async def read_drift_summary(self, tenant_id: str, subscription_id: str) -> DriftSummary | None:
        return await self.drift_repository.read(tenant_id, subscription_id)


def test_classify_drift_stable_when_previous_missing() -> None:
    current = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    assert classify_drift(None, current) is DriftVerdict.STABLE


def test_classify_drifted_on_new_blocking_failure() -> None:
    previous = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    current = _summary(_result(assertion_id="network.overlap", status=ValidationStatus.FAILED))
    assert classify_drift(previous, current) is DriftVerdict.DRIFTED


def test_classify_recovered_when_blocking_failure_clears() -> None:
    previous = _summary(_result(assertion_id="network.overlap", status=ValidationStatus.FAILED))
    current = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    assert classify_drift(previous, current) is DriftVerdict.RECOVERED


def test_drift_watch_enabled_uses_zero_as_disabled() -> None:
    assert drift_watch_enabled(DEFAULT_DRIFT_INTERVAL_SECONDS) is True
    assert drift_watch_enabled(1) is True
    assert drift_watch_enabled(0) is False


async def test_target_failure_isolated_to_one_subscription(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(
        _tenant(
            TENANT_ID,
            subscriptions=(
                SubscriptionEntitlement(subscription_id=SUBSCRIPTION_ID, display_name="sub-a"),
                SubscriptionEntitlement(
                    subscription_id=OTHER_SUBSCRIPTION_ID, display_name="sub-b"
                ),
            ),
        )
    )
    engine = _SequencedReadinessEngine(
        {
            (TENANT_ID, SUBSCRIPTION_ID): RuntimeError("boom"),
            (TENANT_ID, OTHER_SUBSCRIPTION_ID): [
                _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
            ],
        }
    )

    with caplog.at_level(logging.ERROR):
        outcomes = await poll_once(
            tenant_registry=harness.tenant_registry,
            tenant_repository=harness.tenant_repository,
            drift_repository=harness.drift_repository,
            readiness_engine=engine,
            readiness_settings=harness.readiness_settings,
            credential_factory=harness.credential_factory,
            now_fn=lambda: NOW,
        )

    assert [outcome.subscription_id for outcome in outcomes] == [OTHER_SUBSCRIPTION_ID]
    assert any(record.message == "drift evaluation target failed" for record in caplog.records)


async def test_two_cycles_persist_and_transition_verdicts() -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(
        _tenant(
            TENANT_ID,
            subscriptions=(
                SubscriptionEntitlement(
                    subscription_id=SUBSCRIPTION_ID,
                    display_name="sub",
                ),
            ),
        )
    )
    stable = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    drifted = _summary(_result(assertion_id="network.overlap", status=ValidationStatus.FAILED))
    recovered = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    engine = _SequencedReadinessEngine({(TENANT_ID, SUBSCRIPTION_ID): [stable, drifted, recovered]})

    first = await poll_once(
        tenant_registry=harness.tenant_registry,
        tenant_repository=harness.tenant_repository,
        drift_repository=harness.drift_repository,
        readiness_engine=engine,
        readiness_settings=harness.readiness_settings,
        credential_factory=harness.credential_factory,
        now_fn=lambda: NOW,
    )
    second = await poll_once(
        tenant_registry=harness.tenant_registry,
        tenant_repository=harness.tenant_repository,
        drift_repository=harness.drift_repository,
        readiness_engine=engine,
        readiness_settings=harness.readiness_settings,
        credential_factory=harness.credential_factory,
        now_fn=lambda: NOW,
    )
    third = await poll_once(
        tenant_registry=harness.tenant_registry,
        tenant_repository=harness.tenant_repository,
        drift_repository=harness.drift_repository,
        readiness_engine=engine,
        readiness_settings=harness.readiness_settings,
        credential_factory=harness.credential_factory,
        now_fn=lambda: NOW,
    )

    assert first[0].verdict is DriftVerdict.STABLE
    assert second[0].verdict is DriftVerdict.DRIFTED
    assert third[0].verdict is DriftVerdict.RECOVERED

    persisted = await harness.read_drift_summary(TENANT_ID, SUBSCRIPTION_ID)
    assert persisted is not None
    round_tripped = DriftSummary.model_validate(persisted.model_dump())
    assert round_tripped == persisted
    assert round_tripped.verdict is DriftVerdict.RECOVERED
    assert round_tripped.summary == recovered


async def test_drift_notifier_fires_only_on_the_drifted_cycle() -> None:
    """customer-journey-map.md Near-Term improvement, 2026-09-13: drift_watch.py detects drift on
    a schedule but had no code path to the customer. The notifier must fire exactly once, on the
    cycle where the verdict actually becomes DRIFTED — not on the stable cycle before it, nor the
    recovered cycle after."""
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(
        _tenant(
            TENANT_ID,
            subscriptions=(
                SubscriptionEntitlement(subscription_id=SUBSCRIPTION_ID, display_name="sub"),
            ),
            notification_email="customer@example.invalid",
        )
    )
    stable = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    drifted = _summary(_result(assertion_id="network.overlap", status=ValidationStatus.FAILED))
    recovered = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    engine = _SequencedReadinessEngine({(TENANT_ID, SUBSCRIPTION_ID): [stable, drifted, recovered]})
    notifier = _FakeDriftNotifier()

    for _ in range(3):
        await poll_once(
            tenant_registry=harness.tenant_registry,
            tenant_repository=harness.tenant_repository,
            drift_repository=harness.drift_repository,
            readiness_engine=engine,
            readiness_settings=harness.readiness_settings,
            credential_factory=harness.credential_factory,
            now_fn=lambda: NOW,
            notifier=notifier,
        )

    assert len(notifier.calls) == 1
    call = notifier.calls[0]
    assert call["tenant_id"] == TENANT_ID
    assert call["subscription_id"] == SUBSCRIPTION_ID
    assert call["region"] == "australiaeast"
    assert call["blocking_failed_count"] == 1
    assert call["recipient_email"] == "customer@example.invalid"


async def test_drift_notification_skipped_without_recorded_email(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(
        _tenant(
            TENANT_ID,
            subscriptions=(
                SubscriptionEntitlement(subscription_id=SUBSCRIPTION_ID, display_name="sub"),
            ),
            notification_email=None,
        )
    )
    stable = _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
    drifted = _summary(_result(assertion_id="network.overlap", status=ValidationStatus.FAILED))
    engine = _SequencedReadinessEngine({(TENANT_ID, SUBSCRIPTION_ID): [stable, drifted]})
    notifier = _FakeDriftNotifier()

    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            await poll_once(
                tenant_registry=harness.tenant_registry,
                tenant_repository=harness.tenant_repository,
                drift_repository=harness.drift_repository,
                readiness_engine=engine,
                readiness_settings=harness.readiness_settings,
                credential_factory=harness.credential_factory,
                now_fn=lambda: NOW,
                notifier=notifier,
            )

    assert notifier.calls == []
    assert any(
        record.message == "drift notification skipped: no notification_email recorded for tenant"
        for record in caplog.records
    )


async def test_run_forever_returns_immediately_when_disabled() -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(
        _tenant(
            TENANT_ID,
            subscriptions=(
                SubscriptionEntitlement(
                    subscription_id=SUBSCRIPTION_ID,
                    display_name="sub",
                ),
            ),
        )
    )
    engine = _SequencedReadinessEngine(
        {
            (TENANT_ID, SUBSCRIPTION_ID): [
                _summary(_result(assertion_id="network.ok", status=ValidationStatus.PASSED))
            ]
        }
    )

    from groundwork_orchestrator.engine.drift_watch import run_forever

    await asyncio.wait_for(
        run_forever(
            tenant_registry=harness.tenant_registry,
            tenant_repository=harness.tenant_repository,
            drift_repository=harness.drift_repository,
            readiness_engine=engine,
            readiness_settings=harness.readiness_settings,
            credential_factory=harness.credential_factory,
            now_fn=lambda: NOW,
            interval_seconds=0,
        ),
        timeout=0.1,
    )
    assert engine.calls == []


async def test_zero_target_cycle_still_emits_cycle_heartbeat(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The cycle-level event fires even with zero eligible targets, so the absence alert
    (alert-gw-drift-loop-heartbeat) means 'loop dead', never 'no customers yet'."""
    import logging

    harness = _Harness(tenant_ids=[])
    engine = _SequencedReadinessEngine(summaries=[])

    with caplog.at_level(logging.INFO, logger="groundwork_orchestrator.engine.drift_watch"):
        await poll_once(
            tenant_registry=harness.tenant_registry,
            tenant_repository=harness.tenant_repository,
            drift_repository=harness.drift_repository,
            readiness_engine=engine,
            readiness_settings=harness.readiness_settings,
            credential_factory=harness.credential_factory,
            now_fn=lambda: NOW,
        )

    heartbeat = [
        r for r in caplog.records if getattr(r, "event", None) == "drift_evaluation_cycle_completed"
    ]
    assert heartbeat, "expected cycle heartbeat on an empty registry"
    assert heartbeat[0].targets_evaluated == 0
