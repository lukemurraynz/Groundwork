"""The orchestrator execution worker (T022).

Entry point referenced by ``docker/orchestrator.Dockerfile``. Mirrors
``groundwork_controlplane.api.main``'s startup discipline — configuration validates before
anything else, health reflects real dependency state, nothing here is a stand-in for the real
Cosmos client — but this module owns none of the planning, validation, or agent surface the
deterministic-execution boundary reserves for the control plane. There is no model import
anywhere in ``groundwork_orchestrator``
(enforced by ``tests/unit/test_import_boundaries.py``), and this file is no exception.

Alongside the two health routes ``k8s/orchestrator/deployment.tmpl.yaml``'s probes depend on, this
module owns the deployment-queue consumption loop
(:mod:`groundwork_orchestrator.engine.queue_loop`) — the piece ``api/deployments.py``'s own
docstring already disclosed was missing: nothing consumed the ``deployments`` queue at all. All nine
execution stages and every piece the loop wires together (tenant discovery, per-subscription lease,
the sequencer, the blueprint) already existed and were already tested; this file's job is only
construction and lifecycle — build each dependency once at startup with real workload-identity
credentials, start the loop as a background task, and cancel it cleanly on shutdown.

**Two configuration facts shape when the loop actually starts.** ``fabric_capacity_admin_upn`` is a
required setting (``OrchestratorSettings.fabric_capacity_admin_upn``) — a worker with no configured
value refuses to start at all, matching this platform's fail-fast discipline, since Fabric
capacity is billable to the customer and guessing at an admin UPN is not an option (see
``stages/fabric.py``'s own module docstring). ``devops_organization_url`` is genuinely optional
(``OrchestratorSettings.devops_organization_url``'s own docstring) — a newly onboarded tenant may
not have connected an Azure DevOps organization yet. Because ``devops_project`` and ``identity`` are
unconditional blueprint stages every deployment this release must pass through, an absent
organization URL means the queue loop cannot construct a working stage registry at all; rather than
build one against a placeholder that would only fail loudly on first real HTTP call, this module
declines to start the loop and logs why, leaving health routes to keep serving.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

import httpx
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import AzureError
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from groundwork_contracts.deployment import Deployment
from groundwork_contracts.plan import DeploymentPlan
from groundwork_contracts.tenant import CustomerTenant
from groundwork_orchestrator.engine.cost_preflight import CostPreflight
from groundwork_orchestrator.engine.drift_watch import (
    DEFAULT_DRIFT_INTERVAL_SECONDS,
    drift_watch_enabled,
)
from groundwork_orchestrator.engine.drift_watch import run_forever as run_drift_watch_forever
from groundwork_orchestrator.engine.preflight import recheck_no_denying_resource_type_policy
from groundwork_orchestrator.engine.preview import WhatIfCapture, build_what_if_preview_store
from groundwork_orchestrator.engine.queue_loop import DEFAULT_POLL_INTERVAL_SECONDS, run_forever
from groundwork_orchestrator.engine.sequencer import RunResult, Sequencer, Stage
from groundwork_orchestrator.stages.devops_project import DevOpsProjectStage
from groundwork_orchestrator.stages.fabric import FabricStage
from groundwork_orchestrator.stages.identity import IdentityStage
from groundwork_orchestrator.stages.infrastructure import InfrastructureStage
from groundwork_orchestrator.stages.monitoring import MonitoringStage
from groundwork_orchestrator.stages.networking import NetworkingStage
from groundwork_orchestrator.stages.validation_tests import ValidationTestsStage
from groundwork_orchestrator.state.audit_repository import audit_repository
from groundwork_orchestrator.state.cosmos import DEFAULT_DATABASE_NAME, CosmosStateStore
from groundwork_orchestrator.state.report_archive import build_report_archive_store
from groundwork_orchestrator.state.repositories import (
    customer_tenant_repository,
    deployment_repository,
    drift_summary_repository,
    plan_repository,
    report_repository,
    stage_record_repository,
    tenant_registry,
)
from groundwork_shared.config.blueprints import load_catalogue
from groundwork_shared.config.readiness import load_landing_zone_contract
from groundwork_shared.config.settings import (
    ConfigurationError,
    OrchestratorSettings,
    ReadinessSettings,
)
from groundwork_shared.costing.retail_prices import RetailPricesClient
from groundwork_shared.health import CheckResult, CheckStatus, HealthRegistry, liveness
from groundwork_shared.identity.credentials import TenantScopedCredentialFactory
from groundwork_shared.notify.dispatcher import NotificationDispatcher, build_email_sender
from groundwork_shared.queue.subscription_lease import SubscriptionLeaseStore
from groundwork_shared.telemetry.correlation import CorrelationIdLogFilter
from groundwork_shared.telemetry.otel import configure_telemetry
from groundwork_shared.telemetry.scrubbing import ScrubbingFilter, scrub_text
from groundwork_shared.validation.engine import ReadinessEngine
from groundwork_shared.validation.registry import CHECKS

logger = logging.getLogger(__name__)


class _BlueprintSequencerRegistry:
    """Route a deployment run to the sequencer built for its approved blueprint."""

    def __init__(self, sequencers: dict[str, Sequencer]) -> None:
        self._sequencers = sequencers

    async def run(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        *,
        credential: AsyncTokenCredential,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult:
        sequencer = self._sequencers.get(plan.blueprint_id)
        if sequencer is None:
            raise ValueError(f"no sequencer registered for blueprint {plan.blueprint_id!r}")
        return await sequencer.run(
            deployment,
            plan,
            credential=credential,
            devops_organization_url=devops_organization_url,
            fabric_capacity_admin_upn=fabric_capacity_admin_upn,
        )


class _TenantNotifier:
    """Concrete ``NotifierLike`` adapter that resolves the recipient from ``CustomerTenant``.

    ``CustomerTenant.notification_email`` is gathered from the customer during conversation and
    explicitly reconfirmed (via ``ClarificationTracker.NOTIFICATION_EMAIL``). If it has not been
    recorded, the notification is skipped with a warning — not silently dropped to a default, and
    not an error that would mask the already-persisted deployment outcome.
    """

    def __init__(
        self,
        *,
        tenant_repo: _TenantRepositoryLike,
        dispatcher: NotificationDispatcher,
    ) -> None:
        self._tenant_repo = tenant_repo
        self._dispatcher = dispatcher

    async def notify_deployment_outcome(
        self,
        report: object,
        plan: object,
        *,
        tenant_id: str,
        now: object,
    ) -> None:
        from groundwork_contracts.audit import DeploymentReport
        from groundwork_contracts.plan import DeploymentPlan

        if not isinstance(report, DeploymentReport):
            raise TypeError(f"expected DeploymentReport, got {type(report).__name__}")
        if not isinstance(plan, DeploymentPlan):
            raise TypeError(f"expected DeploymentPlan, got {type(plan).__name__}")

        tenant = await self._tenant_repo.read(tenant_id, tenant_id)
        if tenant is None or tenant.notification_email is None:
            logger.warning(
                "notification skipped: no notification_email recorded for tenant",
                extra={
                    "component": "orchestrator",
                    "operation": "notification_dispatch",
                    "tenant_id": tenant_id,
                    "deployment_id": report.deployment_id,
                },
            )
            return
        display_name = tenant.contact_display_name or tenant.display_name
        await self._dispatcher.notify_deployment_outcome(
            report,
            recipient_email=tenant.notification_email,
            recipient_display_name=display_name,
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
    ) -> None:
        """Satisfies ``drift_watch.py``'s ``DriftNotifierLike`` — the recipient is already
        resolved by the caller (``_evaluate_target`` holds the full ``CustomerTenant``), so this
        is a thin pass-through to the dispatcher, unlike ``notify_deployment_outcome`` above
        which does its own repository lookup because ``Sequencer`` only has a ``tenant_id``."""
        await self._dispatcher.notify_drift_detected(
            tenant_id=tenant_id,
            subscription_id=subscription_id,
            region=region,
            blocking_failed_count=blocking_failed_count,
            recipient_email=recipient_email,
            recipient_display_name=recipient_display_name,
        )


class _TenantRepositoryLike(Protocol):
    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None: ...


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.addFilter(ScrubbingFilter())
    handler.addFilter(CorrelationIdLogFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    _configure_logging()

    settings = OrchestratorSettings.from_environment()
    app.state.settings = settings

    configure_telemetry(
        connection_string=settings.applicationinsights_connection_string,
        service_name="groundwork-orchestrator",
    )

    credential = DefaultAzureCredential()
    app.state.credential = credential
    cosmos_client = CosmosClient(settings.cosmos_endpoint, credential=credential)
    app.state.cosmos_client = cosmos_client

    state_store = CosmosStateStore(cosmos_client)
    lease_store = SubscriptionLeaseStore(
        cosmos_client.get_database_client(DEFAULT_DATABASE_NAME).get_container_client(
            "subscription_leases"
        )
    )
    # One shared client for every raw-REST stage (devops_project, infrastructure, networking,
    # identity, fabric, monitoring, validation_tests all accept an injected httpx.AsyncClient
    # precisely so callers do not have to give each its own connection pool). Passing it explicitly
    # means every stage's own `_owns_client` is False, so `Stage.aclose()` is a no-op for all of
    # them and this client's lifecycle is owned in exactly one place.
    stage_http_client = httpx.AsyncClient(timeout=30.0)
    app.state.http_client = stage_http_client

    queue_task: asyncio.Task[None] | None = None
    drift_task: asyncio.Task[None] | None = None
    blueprints_root = Path(settings.blueprints_path or "infra/blueprints")
    catalogue = load_catalogue(blueprints_root)
    drift_blueprint_id = (
        "standard-production-fabric"
        if "standard-production-fabric" in catalogue
        else next(iter(catalogue))
    )
    contract = load_landing_zone_contract(blueprints_root / drift_blueprint_id / "assertions.yaml")
    readiness_engine = ReadinessEngine(contract, CHECKS)
    readiness_settings = ReadinessSettings(
        orchestrator_principal_id=settings.orchestrator_principal_id,
        devops_organization_url=settings.devops_organization_url,
    )

    # Per-tenant engagement data (the customer's own DevOps organization URL and Fabric capacity
    # admin UPN) is resolved per deployment by the queue loop — tenant record first, the
    # worker-wide settings below as fallback. Stages are constructed without per-engagement
    # values; each resolves from StageExecutionContext at execution time and raises a named error
    # if neither source provided one (a backstop the queue loop's per-deployment decline normally
    # prevents from ever firing).
    stages: dict[str, Stage] = {
        "devops_project": DevOpsProjectStage(
            customer_tenant_repository=customer_tenant_repository(state_store),
            organization_url=settings.devops_organization_url,
            http_client=stage_http_client,
        ),
        "infrastructure": InfrastructureStage(http_client=stage_http_client),
        "networking": NetworkingStage(http_client=stage_http_client),
        "identity": IdentityStage(http_client=stage_http_client),
        "fabric": FabricStage(
            capacity_admin_upn=settings.fabric_capacity_admin_upn,
            validation_principal_object_id=settings.fabric_validator_object_id,
            organization_url=settings.devops_organization_url,
            http_client=stage_http_client,
        ),
        "monitoring": MonitoringStage(http_client=stage_http_client),
        "validation_tests": ValidationTestsStage(http_client=stage_http_client),
    }

    what_if_preview_store = build_what_if_preview_store(
        storage_account_url=settings.storage_account_url, credential=credential
    )
    what_if_capture = WhatIfCapture(http_client=stage_http_client, store=what_if_preview_store)

    # T075a: reuses stage_http_client (already closed once at shutdown, below) rather than
    # managing a second httpx.AsyncClient - the Retail Prices API is public/unauthenticated,
    # so sharing the client carries no credential-scoping risk.
    retail_prices_client = RetailPricesClient(client=stage_http_client)
    cost_preflight = CostPreflight(
        retail_prices_client=retail_prices_client, governance=settings.governance
    )

    # T092/T093: report generation, triggered by Sequencer itself the moment a deployment
    # reaches a terminal, reportable state.
    report_archive_store = build_report_archive_store(
        storage_account_url=settings.storage_account_url, credential=credential
    )

    # Wire email notification when ACS Email is configured - optional, matching
    # devops_organization_url's pattern. When absent, the Sequencer runs without a notifier.
    notifier: _TenantNotifier | None = None
    if settings.acs_email_endpoint and settings.acs_email_sender_address:
        email_sender = build_email_sender(
            acs_endpoint=settings.acs_email_endpoint,
            sender_address=settings.acs_email_sender_address,
            credential=credential,
        )
        notifier = _TenantNotifier(
            tenant_repo=customer_tenant_repository(state_store),
            dispatcher=NotificationDispatcher(email_sender=email_sender),
        )

    sequencer = _BlueprintSequencerRegistry(
        {
            blueprint_id: Sequencer(
                blueprint,
                stages,
                what_if_capture,
                deployment_repository=deployment_repository(state_store),
                stage_record_repository=stage_record_repository(state_store),
                audit_repository=audit_repository(state_store),
                actor_object_id=settings.orchestrator_principal_id,
                now_fn=lambda: datetime.now(UTC),
                tenant_repository=customer_tenant_repository(state_store),
                policy_preflight=recheck_no_denying_resource_type_policy,
                cost_preflight=cost_preflight,
                report_archive_store=report_archive_store,
                report_repository=report_repository(state_store),
                notifier=notifier,
            )
            for blueprint_id, blueprint in catalogue.items()
        }
    )
    # One factory for the process, per TenantScopedCredentialFactory's own docstring: this
    # release's identity model is one workload identity per component, not per tenant. The
    # `credential` constructed above (the async DefaultAzureCredential) is passed in explicitly
    # so the factory never falls back to its own module-level default, which is the
    # *synchronous* DefaultAzureCredential - incompatible with every stage's
    # `await credential.get_token(...)` call.
    credential_factory = TenantScopedCredentialFactory(credential=credential)

    queue_task = asyncio.create_task(
        run_forever(
            tenant_registry=tenant_registry(state_store),
            deployment_repository=deployment_repository(state_store),
            plan_repository=plan_repository(state_store),
            customer_tenant_repository=customer_tenant_repository(state_store),
            lease_store=lease_store,
            sequencer=sequencer,
            credential_factory=credential_factory,
            now_fn=lambda: datetime.now(UTC),
            poll_interval_seconds=DEFAULT_POLL_INTERVAL_SECONDS,
            devops_organization_url_fallback=settings.devops_organization_url,
            fabric_capacity_admin_upn_fallback=settings.fabric_capacity_admin_upn,
            max_concurrent_deployments=settings.max_concurrent_deployments,
        ),
        name="groundwork-queue-consumption",
    )
    app.state.queue_task = queue_task
    if drift_watch_enabled(settings.drift_interval_seconds):
        drift_task = asyncio.create_task(
            run_drift_watch_forever(
                tenant_registry=tenant_registry(state_store),
                tenant_repository=customer_tenant_repository(state_store),
                drift_repository=drift_summary_repository(state_store),
                readiness_engine=readiness_engine,
                readiness_settings=readiness_settings,
                credential_factory=credential_factory,
                now_fn=lambda: datetime.now(UTC),
                interval_seconds=settings.drift_interval_seconds,
                notifier=notifier,
            ),
            name="groundwork-drift-watch",
        )
        app.state.drift_task = drift_task
    else:
        logger.info(
            "drift watch disabled",
            extra={
                "component": "orchestrator",
                "operation": "startup",
                "drift_interval_seconds": settings.drift_interval_seconds,
                "default_drift_interval_seconds": DEFAULT_DRIFT_INTERVAL_SECONDS,
            },
        )

    async def _check_cosmos() -> CheckResult:
        try:
            async for _ in cosmos_client.list_databases(max_item_count=1):
                break
            return CheckResult(name="cosmos", status=CheckStatus.HEALTHY, detail="reachable")
        except AzureError as exc:
            return CheckResult(
                name="cosmos",
                status=CheckStatus.UNHEALTHY,
                detail=scrub_text(f"{type(exc).__name__}: {exc}"),
            )

    registry = HealthRegistry()
    registry.register(
        "configuration",
        lambda: CheckResult(
            name="configuration",
            status=CheckStatus.HEALTHY,
            detail=f"validated for environment {settings.environment_name}",
        ),
    )
    registry.register_async("cosmos", _check_cosmos)
    app.state.health = registry

    logger.info(
        "orchestrator worker started",
        extra={
            "component": "orchestrator",
            "operation": "startup",
            "environment": settings.environment_name,
            "allow_tenant_writes": settings.allow_tenant_writes,
        },
    )

    yield

    if queue_task is not None:
        queue_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await queue_task
    if drift_task is not None:
        drift_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await drift_task

    await stage_http_client.aclose()
    await cosmos_client.close()
    await credential.close()

    logger.info(
        "orchestrator worker stopping",
        extra={"component": "orchestrator", "operation": "shutdown"},
    )


app = FastAPI(
    title="Groundwork Orchestrator",
    description="Deterministic deployment execution worker. Never calls a model.",
    lifespan=lifespan,
)


@app.get("/health/live", tags=["health"])
async def health_live() -> JSONResponse:
    return JSONResponse(status_code=200 if liveness() else 503, content={"status": "alive"})


@app.get("/health/ready", tags=["health"])
async def health_ready() -> JSONResponse:
    registry: HealthRegistry | None = getattr(app.state, "health", None)
    if registry is None:
        return JSONResponse(
            status_code=503,
            content={"ready": False, "detail": "startup has not completed"},
        )

    report = await registry.evaluate()
    payload = {
        "ready": report.ready,
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail} for c in report.checks
        ],
    }
    return JSONResponse(status_code=200 if report.ready else 503, content=payload)


@app.exception_handler(ConfigurationError)
async def _configuration_error_handler(_: object, exc: ConfigurationError) -> JSONResponse:
    logger.error("configuration error at request time", exc_info=True)
    return JSONResponse(status_code=500, content={"detail": "worker configuration is invalid"})
