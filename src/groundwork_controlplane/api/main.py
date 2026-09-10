"""Control-plane FastAPI application.

Entry point referenced by ``docker/controlplane.Dockerfile``. Kept thin: configuration loads at
startup and fails fast, health checks are registered, and routes are mounted. No domain logic lives
here.

Startup order is deliberate. Configuration is validated before anything else, so a misconfigured
pod fails immediately with a named key rather than starting and failing on first request — the
difference between a deployment error and a customer-visible incident.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from azure.core.exceptions import AzureError
from azure.cosmos.aio import CosmosClient
from azure.identity.aio import DefaultAzureCredential
from azure.keyvault.secrets.aio import SecretClient
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from groundwork_channels.voice.consent import build_consent_store
from groundwork_channels.voice.enablement import VoiceEnablementGate
from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_controlplane.agents.planning import PlanningAgent, build_planning_agent
from groundwork_controlplane.agents.providers.foundry_openai import create_foundry_chat_client
from groundwork_controlplane.api.approvals import router as approvals_router
from groundwork_controlplane.api.auth import TokenPolicy, TokenValidator, refresh_cross_tenant_auth
from groundwork_controlplane.api.correlation import CorrelationIdMiddleware
from groundwork_controlplane.api.deployments import router as deployments_router
from groundwork_controlplane.api.entra_decoder import MultiTenantTokenDecoder
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.lighthouse_onboarding import router as lighthouse_onboarding_router
from groundwork_controlplane.api.plans import router as plans_router
from groundwork_controlplane.api.readiness_reports import router as readiness_reports_router
from groundwork_controlplane.api.recovery import router as recovery_router
from groundwork_controlplane.api.reports import router as reports_router
from groundwork_controlplane.api.tenants import router as tenants_router
from groundwork_controlplane.api.voice import _EnablementRateLimiter
from groundwork_controlplane.api.voice import router as voice_router
from groundwork_controlplane.approval.artefacts import build_approval_artefact_store
from groundwork_controlplane.validation.engine import ReadinessEngine
from groundwork_controlplane.validation.registry import CHECKS
from groundwork_orchestrator.state.cosmos import CosmosStateStore
from groundwork_orchestrator.state.repositories import (
    approval_repository,
    conversation_repository,
    customer_tenant_repository,
    deployment_repository,
    drift_summary_repository,
    pending_approval_repository,
    plan_repository,
    report_repository,
    stage_record_repository,
    tenant_registry,
)
from groundwork_shared.config.blueprints import BlueprintLoadError, load_catalogue
from groundwork_shared.config.readiness import load_landing_zone_contract
from groundwork_shared.config.settings import ConfigurationError, Settings
from groundwork_shared.costing.retail_prices import RetailPricesClient
from groundwork_shared.health import CheckResult, CheckStatus, HealthRegistry, liveness
from groundwork_shared.telemetry.correlation import CorrelationIdLogFilter
from groundwork_shared.telemetry.otel import configure_telemetry
from groundwork_shared.telemetry.scrubbing import ScrubbingFilter, scrub_text

logger = logging.getLogger(__name__)

API_VERSION = "2026-07-30"


def _build_blueprint_runtime(
    *,
    catalogue: dict[str, PlatformBlueprint],
    blueprints_path: Path,
    settings: Settings,
    credential: DefaultAzureCredential,
    retail_prices_client: RetailPricesClient,
) -> tuple[dict[str, ReadinessEngine], dict[str, PlanningAgent]]:
    """Build per-blueprint readiness and planning runtime state."""
    readiness_engines: dict[str, ReadinessEngine] = {}
    planning_agents: dict[str, PlanningAgent] = {}
    for blueprint_id, blueprint in catalogue.items():
        contract = load_landing_zone_contract(blueprints_path / blueprint_id / "assertions.yaml")
        readiness_engines[blueprint_id] = ReadinessEngine(contract, CHECKS)
        planning_agents[blueprint_id] = build_planning_agent(
            project_endpoint=settings.foundry.project_endpoint,
            model_deployment=settings.foundry.model_deployment,
            credential=credential,
            blueprint=blueprint,
            retail_prices_client=retail_prices_client,
        )
    return readiness_engines, planning_agents


def _default_blueprint_id(catalogue: dict[str, PlatformBlueprint]) -> str:
    """Compatibility default for still-single-blueprint call sites.

    Prefer the long-shipped standard blueprint when present so legacy voice/chat paths keep the
    pre-ADR-0010 behaviour until they become blueprint-aware too.
    """
    if "standard-production-fabric" in catalogue:
        return "standard-production-fabric"
    return next(iter(catalogue))


def _configure_logging() -> None:
    """Attach the secret-scrubbing and correlation-ID filters at the handler.

    At the handler rather than per-logger so a module that logs without thinking about redaction
    still cannot leak (FR-049), and so every line — not only ones a caller remembers to annotate —
    carries the request it belongs to (FR-048).
    """
    handler = logging.StreamHandler()
    handler.addFilter(ScrubbingFilter())
    handler.addFilter(CorrelationIdLogFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s [%(correlation_id)s] %(name)s: %(message)s")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Validate configuration and load the blueprint catalogue before serving.

    Both are fail-fast. A control plane that starts without a blueprint catalogue can accept plan
    requests it has no approved topology to satisfy, which is a worse failure than refusing to
    start.
    """
    _configure_logging()

    settings = Settings.from_environment()
    app.state.settings = settings

    # T017: real Azure Monitor export when configured, a documented degraded state (console only)
    # when not — never a stub tracer standing in for either.
    configure_telemetry(
        connection_string=settings.applicationinsights_connection_string,
        service_name="groundwork-controlplane",
    )

    blueprints_path = Path(getattr(settings, "blueprints_path", None) or "infra/blueprints")
    catalogue = load_catalogue(blueprints_path)
    app.state.blueprints = catalogue

    # Real Entra token validation, wired to the application scripts/postprovision.ps1 created
    # (FR-005, FR-006, FR-007). The decoder fetches actual signing keys per trusted tenant —
    # nothing here is a stand-in for cryptographic verification.
    #
    # Cross-tenant customers (FR-006, 2026-08-25): the decoder routes on the token's own `tid` to
    # the right tenant's JWKS, and the issuer allow-list is seeded with the home tenant plus every
    # *already-onboarded* customer tenant from the same registry the rest of the API reads.
    # Customers who onboard later are picked up by `_refresh_issuer_loop` below - a tenant that
    # was never onboarded never enters this set.
    credential = DefaultAzureCredential()
    app.state.credential = credential
    cosmos_client = CosmosClient(settings.cosmos_endpoint, credential=credential)
    app.state.cosmos_client = cosmos_client

    state_store = CosmosStateStore(cosmos_client)
    registry = tenant_registry(state_store)
    onboarded = [tid async for tid in registry.list_tenant_ids()]

    multi_decoder = MultiTenantTokenDecoder(home_tenant_id=settings.entra.tenant_id)
    policy = TokenPolicy(
        expected_audience=settings.entra.audience,
        allowed_issuers=frozenset(
            {
                f"https://login.microsoftonline.com/{settings.entra.tenant_id}/v2.0",
                f"https://sts.windows.net/{settings.entra.tenant_id}/",
            }
        ),
    )
    for tenant_id in onboarded:
        if tenant_id != settings.entra.tenant_id:
            multi_decoder.register_tenant(tenant_id)
            policy.add_tenant(tenant_id)
    app.state.multi_tenant_decoder = multi_decoder

    app.state.token_validator = TokenValidator(decoder=multi_decoder, policy=policy)

    # FR-006 cross-tenant auth: re-read the onboarded registry every 60s so a customer who
    # completes onboarding after this pod started can authenticate without a restart. Idempotent;
    # failures are logged and retried next cycle - a transient Cosmos hiccup must not kill auth.
    import asyncio as _asyncio

    async def _refresh_issuer_loop() -> None:

        while True:
            try:
                ids = [tid async for tid in registry.list_tenant_ids()]
                newly = refresh_cross_tenant_auth(multi_decoder.register_tenant, policy, ids)
                if newly:
                    logger.info(
                        "cross-tenant issuers refreshed",
                        extra={
                            "component": "controlplane",
                            "operation": "auth_issuer_refresh",
                            "new_tenants": newly,
                            "trusted_tenants": len(ids),
                        },
                    )
            except Exception:
                logger.exception("issuer allow-list refresh failed; retrying next cycle")
            await _asyncio.sleep(60)

    app.state.issuer_refresh_task = _asyncio.create_task(_refresh_issuer_loop())
    key_vault_client = SecretClient(vault_url=settings.key_vault_uri, credential=credential)
    app.state.key_vault_client = key_vault_client

    retail_prices_client = RetailPricesClient()
    app.state.retail_prices_client = retail_prices_client
    readiness_engines, planning_agents = _build_blueprint_runtime(
        catalogue=catalogue,
        blueprints_path=blueprints_path,
        settings=settings,
        credential=credential,
        retail_prices_client=retail_prices_client,
    )
    default_blueprint_id = _default_blueprint_id(catalogue)
    app.state.readiness_engines = readiness_engines
    app.state.planning_agents = planning_agents
    app.state.readiness_engine = readiness_engines[default_blueprint_id]
    app.state.planning_agent = planning_agents[default_blueprint_id]
    # A separate client instance from planning_agent's own — see
    # groundwork_controlplane.agents.providers.foundry_openai's module docstring. /chat dispatches
    # tool calls itself (to real business-logic functions, not a Python callable the framework
    # would invoke), so its auto function-invocation loop is disabled.
    app.state.voice_tool_client = create_foundry_chat_client(
        project_endpoint=settings.foundry.project_endpoint,
        credential=credential,
        model=settings.foundry.model_deployment,
    )
    app.state.voice_tool_client.function_invocation_configuration["enabled"] = False

    app.state.plan_repository = plan_repository(state_store)
    app.state.tenant_repository = customer_tenant_repository(state_store)
    app.state.conversation_repository = conversation_repository(state_store)
    app.state.approval_repository = approval_repository(state_store)
    app.state.pending_approval_repository = pending_approval_repository(state_store)
    app.state.deployment_repository = deployment_repository(state_store)
    app.state.stage_record_repository = stage_record_repository(state_store)
    app.state.report_repository = report_repository(state_store)
    app.state.drift_summary_repository = drift_summary_repository(state_store)

    # T067: approval evidence in the immutable `approvals` blob container
    # infra/modules/storage.bicep provisions.
    app.state.approval_artefact_store = build_approval_artefact_store(
        storage_account_url=settings.storage_account_url, credential=credential
    )
    # T099: consent artefact store — same immutable blob pattern, separate container.
    app.state.consent_store = build_consent_store(
        storage_account_url=settings.storage_account_url, credential=credential
    )
    # T100: voice enablement gate checks CustomerTenant per tenant.
    app.state.voice_gate = VoiceEnablementGate(app.state.tenant_repository)
    app.state.enablement_limiter = _EnablementRateLimiter(now_fn=lambda: datetime.now(UTC))
    # T099/T106: voice and approval handlers need a deterministic clock.
    app.state.now_fn = lambda: datetime.now(UTC)

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

    async def _check_key_vault() -> CheckResult:
        try:
            async for _ in key_vault_client.list_properties_of_secrets(max_page_size=1):
                break
            return CheckResult(name="key-vault", status=CheckStatus.HEALTHY, detail="reachable")
        except AzureError as exc:
            return CheckResult(
                name="key-vault",
                status=CheckStatus.UNHEALTHY,
                detail=scrub_text(f"{type(exc).__name__}: {exc}"),
            )

    health_registry = HealthRegistry()
    health_registry.register(
        "configuration",
        lambda: CheckResult(
            name="configuration",
            status=CheckStatus.HEALTHY,
            detail=f"validated for environment {settings.environment_name}",
        ),
    )
    health_registry.register(
        "blueprint-catalogue",
        lambda: CheckResult(
            name="blueprint-catalogue",
            status=CheckStatus.HEALTHY,
            detail=f"{len(catalogue)} approved blueprint(s) loaded",
        ),
    )
    health_registry.register_async("cosmos", _check_cosmos)
    health_registry.register_async("key-vault", _check_key_vault)
    app.state.health = health_registry

    logger.info(
        "control plane started",
        extra={
            "component": "controlplane",
            "operation": "startup",
            "environment": settings.environment_name,
            "blueprints": len(catalogue),
        },
    )

    yield

    await retail_prices_client.close()
    await cosmos_client.close()
    await key_vault_client.close()
    await credential.close()

    logger.info(
        "control plane stopping",
        extra={"component": "controlplane", "operation": "shutdown"},
    )


app = FastAPI(
    title="Groundwork Control Plane",
    version=API_VERSION,
    description=(
        "Conversational data platform provisioning. Every capability exposed here is reachable "
        "without a conversation, which is what the deterministic-execution boundary requires."
    ),
    lifespan=lifespan,
)
# T019 / FR-048: every request gets a correlation ID, caller-supplied (if a well-formed GUID) or
# freshly minted, propagated through logging via CorrelationIdLogFilter and echoed on the response.
app.add_middleware(CorrelationIdMiddleware)
# T049: the plans API (POST /plans, GET /plans/{planId}, POST /plans/{planId}/validate).
app.include_router(plans_router)
# T067: POST /plans/{planId}/approvals.
app.include_router(approvals_router)
# T084: POST /deployments, GET /deployments/{deploymentId} — admission and the status monitor.
app.include_router(deployments_router)
# T085: POST /deployments/{deploymentId}/recovery — retry, forward-fix, or (approval-gated) rollback
# for a halted deployment.
app.include_router(recovery_router)
# T095: GET /deployments/{deploymentId}/report — the read side of report generation
# (engine/report_builder.py, state/report_archive.py, T092/T093), triggered by Sequencer itself.
app.include_router(reports_router)
# FR-006: operator-driven tenant onboarding — create, read, and confirm consent.
app.include_router(tenants_router)
# FR-006/FR-038b: read-only customer onboarding facts for Lighthouse delegation + ADO org access.
app.include_router(lighthouse_onboarding_router)
# Operator-forwardable readiness assessment for onboarding/drift review.
app.include_router(readiness_reports_router)
# T099-T107: voice channel — consent, enablement, voice-to-plan bridge.
app.include_router(voice_router)


# Serve static files (voice.html browser UI)
class _NoCacheStaticFiles(StaticFiles):
    """`Cache-Control: no-cache` on every response — found live 2026-08-23: FastAPI's default
    `StaticFiles` sets no `Cache-Control` at all, leaving cache behaviour up to browser heuristics,
    and a browser reusing a stale cached copy of ``voice.html`` after a redeploy produced a run of
    genuinely confusing auth errors (wrong redirect URI, wrong MSAL config) that were actually
    "old page, new server" rather than real bugs. `no-cache` (not `no-store`) still allows a fast
    conditional-GET/304 for unchanged content via the existing ETag — it just forbids serving a
    cached body without revalidating first, which is exactly what a redeploy needs."""

    async def get_response(self, path: str, scope: Any) -> Any:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


static_dir = Path(__file__).resolve().parent.parent / "static"
if static_dir.is_dir():
    app.mount("/static", _NoCacheStaticFiles(directory=str(static_dir), html=True), name="static")
# T051: every domain exception raised by a route above (and by FastAPI's own request-body
# validation) becomes an RFC 9457 application/problem+json response. Registered once, here, rather
# than duplicated per router.
register_error_handlers(app)


@app.get("/health/live", tags=["health"])
async def health_live() -> JSONResponse:
    """Liveness. Does not consult dependencies — see health.liveness for why."""
    return JSONResponse(status_code=200 if liveness() else 503, content={"status": "alive"})


@app.get("/health/ready", tags=["health"])
async def health_ready() -> JSONResponse:
    """Readiness, reflecting real dependency health (FR-042).

    Returns 503 while any registered dependency is unhealthy. It must never report ready
    unconditionally — that would make the probe a decoration rather than a control.
    """
    registry: HealthRegistry | None = getattr(app.state, "health", None)
    if registry is None:
        # Startup has not completed. Not-ready is the truthful answer.
        return JSONResponse(
            status_code=503,
            content={"ready": False, "detail": "startup has not completed"},
        )

    report = await registry.evaluate()
    payload: dict[str, Any] = {
        "ready": report.ready,
        "checks": [
            {"name": c.name, "status": c.status.value, "detail": c.detail} for c in report.checks
        ],
    }
    return JSONResponse(status_code=200 if report.ready else 503, content=payload)


@app.get("/v1/blueprints", tags=["blueprints"])
async def list_blueprints() -> JSONResponse:
    """Approved platform blueprints (read-only).

    Part of the FR-033 parity surface: a caller can discover what can be deployed without holding
    a conversation.
    """
    catalogue = getattr(app.state, "blueprints", {})
    return JSONResponse(
        content={
            "blueprints": [
                {
                    "blueprintId": bp.blueprint_id,
                    "version": bp.version,
                    "displayName": bp.display_name,
                    "defaultFabricSku": bp.default_fabric_sku.value,
                    "requiresPowerBiViewerLicensing": (
                        not bp.default_fabric_sku.supports_free_powerbi_viewers
                    ),
                    "stages": list(bp.execution_order()),
                }
                for bp in catalogue.values()
            ]
        }
    )


@app.exception_handler(ConfigurationError)
async def _configuration_error_handler(_: object, exc: ConfigurationError) -> JSONResponse:
    # Should be unreachable: configuration is validated at startup. Present so that if it ever is
    # reached, it surfaces as a clear 500 rather than an opaque stack trace.
    logger.error("configuration error at request time", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "type": "https://groundwork.invalid/problems/configuration",
            "title": "Service configuration is invalid",
            "status": 500,
        },
    )


@app.exception_handler(BlueprintLoadError)
async def _blueprint_error_handler(_: object, exc: BlueprintLoadError) -> JSONResponse:
    logger.error("blueprint catalogue error", exc_info=True)
    return JSONResponse(
        status_code=500,
        content={
            "type": "https://groundwork.invalid/problems/blueprint-catalogue",
            "title": "Blueprint catalogue is unavailable",
            "status": 500,
        },
    )
