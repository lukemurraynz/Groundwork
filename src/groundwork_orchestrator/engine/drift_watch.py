"""Periodic readiness re-evaluation per tenant/subscription.

Turns the plan-time readiness engine into an ongoing signal the orchestrator can emit without
mutating customer state.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.readiness import DriftSummary, DriftVerdict, ReadinessSummary
from groundwork_contracts.tenant import ConsentState, CustomerTenant
from groundwork_orchestrator.state.cosmos import TenantRegistry
from groundwork_orchestrator.state.repositories import (
    CustomerTenantRepository,
    DriftSummaryRepository,
)
from groundwork_shared.config.settings import ReadinessSettings
from groundwork_shared.telemetry.metrics import record_drift_blocking_failures
from groundwork_shared.validation.engine import ValidationContext
from groundwork_shared.validation.registry import build_validation_context

logger = logging.getLogger(__name__)

DEFAULT_DRIFT_INTERVAL_SECONDS = 900


class ReadinessEvaluator(Protocol):
    async def evaluate(self, context: ValidationContext, *, now: datetime) -> ReadinessSummary: ...


class TenantCredentialLike(Protocol):
    def for_tenant(self, tenant_id: str) -> AsyncTokenCredential: ...


class CredentialFactoryLike(Protocol):
    def scoped_to(self, tenant_id: str) -> TenantCredentialLike: ...


@dataclass(frozen=True, slots=True)
class DriftEvaluationOutcome:
    tenant_id: str
    subscription_id: str
    region: str
    verdict: DriftVerdict
    blocking_failed_count: int
    summary: ReadinessSummary


def drift_watch_enabled(interval_seconds: int) -> bool:
    return interval_seconds > 0


def classify_drift(previous: ReadinessSummary | None, current: ReadinessSummary) -> DriftVerdict:
    if previous is None:
        return DriftVerdict.STABLE
    previous_blocking = {result.assertion_id for result in previous.blocking_failures}
    current_blocking = {result.assertion_id for result in current.blocking_failures}
    if current_blocking - previous_blocking:
        return DriftVerdict.DRIFTED
    if previous_blocking - current_blocking:
        return DriftVerdict.RECOVERED
    return DriftVerdict.STABLE


def _primary_region(tenant: CustomerTenant) -> str:
    return sorted(tenant.approved_regions)[0]


def _entitled_subscription_ids(tenant: CustomerTenant) -> tuple[str, ...]:
    return tuple(entitlement.subscription_id for entitlement in tenant.subscriptions)


async def _evaluate_target(
    *,
    tenant: CustomerTenant,
    subscription_id: str,
    drift_repository: DriftSummaryRepository,
    readiness_engine: ReadinessEvaluator,
    readiness_settings: ReadinessSettings,
    credential_factory: CredentialFactoryLike,
    now_fn: Callable[[], datetime],
) -> DriftEvaluationOutcome:
    region = _primary_region(tenant)
    credential = credential_factory.scoped_to(tenant.tenant_id).for_tenant(tenant.tenant_id)
    context = build_validation_context(
        tenant_id=tenant.tenant_id,
        subscription_id=subscription_id,
        region=region,
        credential=credential,
        readiness=readiness_settings,
        tenant_devops_organization_url=tenant.devops_organization_url,
    )
    now = now_fn()
    current_summary = await readiness_engine.evaluate(context, now=now)
    previous = await drift_repository.read(tenant.tenant_id, subscription_id)
    verdict = classify_drift(previous.summary if previous is not None else None, current_summary)
    stored = DriftSummary(
        tenant_id=tenant.tenant_id,
        subscription_id=subscription_id,
        region=region,
        verdict=verdict,
        summary=current_summary,
    )
    persisted = await drift_repository.replace(tenant.tenant_id, stored)
    blocking_failed_count = len(persisted.summary.blocking_failures)
    record_drift_blocking_failures(
        tenant_id=tenant.tenant_id,
        subscription_id=subscription_id,
        blocking_failures=blocking_failed_count,
    )
    logger.info(
        "drift evaluation completed",
        extra={
            "component": "orchestrator",
            "operation": "drift_watch",
            "event": "drift_evaluation_completed",
            "tenant_id": tenant.tenant_id,
            "subscription_id": subscription_id,
            "verdict": verdict.value,
            "blocking_failed_count": blocking_failed_count,
        },
    )
    if verdict is DriftVerdict.DRIFTED:
        logger.warning(
            "platform drift detected",
            extra={
                "component": "orchestrator",
                "operation": "drift_watch",
                "event": "platform_drift_detected",
                "tenant_id": tenant.tenant_id,
                "subscription_id": subscription_id,
                "verdict": verdict.value,
                "blocking_failed_count": blocking_failed_count,
            },
        )
    return DriftEvaluationOutcome(
        tenant_id=tenant.tenant_id,
        subscription_id=subscription_id,
        region=region,
        verdict=verdict,
        blocking_failed_count=blocking_failed_count,
        summary=persisted.summary,
    )


async def poll_once(
    *,
    tenant_registry: TenantRegistry,
    tenant_repository: CustomerTenantRepository,
    drift_repository: DriftSummaryRepository,
    readiness_engine: ReadinessEvaluator,
    readiness_settings: ReadinessSettings,
    credential_factory: CredentialFactoryLike,
    now_fn: Callable[[], datetime],
) -> list[DriftEvaluationOutcome]:
    outcomes: list[DriftEvaluationOutcome] = []
    async for tenant_id in tenant_registry.list_tenant_ids():
        tenant = await tenant_repository.read(tenant_id, tenant_id)
        if tenant is None:
            logger.warning(
                "drift evaluation skipped: tenant record missing",
                extra={
                    "component": "orchestrator",
                    "operation": "drift_watch",
                    "tenant_id": tenant_id,
                },
            )
            continue
        if tenant.consent_state is not ConsentState.GRANTED:
            logger.info(
                "drift evaluation skipped: tenant consent not granted",
                extra={
                    "component": "orchestrator",
                    "operation": "drift_watch",
                    "tenant_id": tenant.tenant_id,
                    "consent_state": tenant.consent_state.value,
                },
            )
            continue
        for subscription_id in _entitled_subscription_ids(tenant):
            try:
                outcome = await _evaluate_target(
                    tenant=tenant,
                    subscription_id=subscription_id,
                    drift_repository=drift_repository,
                    readiness_engine=readiness_engine,
                    readiness_settings=readiness_settings,
                    credential_factory=credential_factory,
                    now_fn=now_fn,
                )
            except Exception:
                logger.error(
                    "drift evaluation target failed",
                    exc_info=True,
                    extra={
                        "component": "orchestrator",
                        "operation": "drift_watch",
                        "tenant_id": tenant.tenant_id,
                        "subscription_id": subscription_id,
                    },
                )
                continue
            outcomes.append(outcome)

    # Cycle-level heartbeat: emitted even when there are zero eligible targets, so an
    # absence alert on this event means "the loop itself is dead", never "no customers yet".
    logger.info(
        "drift_evaluation_cycle_completed",
        extra={
            "event": "drift_evaluation_cycle_completed",
            "component": "orchestrator",
            "operation": "drift_watch",
            "targets_evaluated": len(outcomes),
        },
    )
    return outcomes


async def run_forever(
    *,
    tenant_registry: TenantRegistry,
    tenant_repository: CustomerTenantRepository,
    drift_repository: DriftSummaryRepository,
    readiness_engine: ReadinessEvaluator,
    readiness_settings: ReadinessSettings,
    credential_factory: CredentialFactoryLike,
    now_fn: Callable[[], datetime],
    interval_seconds: int = DEFAULT_DRIFT_INTERVAL_SECONDS,
) -> None:
    if not drift_watch_enabled(interval_seconds):
        return
    while True:
        try:
            await poll_once(
                tenant_registry=tenant_registry,
                tenant_repository=tenant_repository,
                drift_repository=drift_repository,
                readiness_engine=readiness_engine,
                readiness_settings=readiness_settings,
                credential_factory=credential_factory,
                now_fn=now_fn,
            )
        except Exception:
            logger.error(
                "drift watch cycle failed",
                exc_info=True,
                extra={"component": "orchestrator", "operation": "drift_watch"},
            )
        await asyncio.sleep(interval_seconds)
