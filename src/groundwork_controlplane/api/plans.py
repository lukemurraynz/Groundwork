"""The plans API (T049) — FR-033's non-conversational path to Story 1's planning capability.

Same planning path, same validators, same schema boundary as the conversational channels: every
route here calls exactly the same :class:`~groundwork_controlplane.agents.planning.PlanningAgent`
and :class:`~groundwork_controlplane.validation.engine.ReadinessEngine` a Teams or voice turn would,
because the deterministic-execution boundary requires the orchestration seam not have a second,
looser entry point.

``tenant_id`` is never read from the request body anywhere in this module — every route takes it
from :class:`~groundwork_controlplane.api.auth.AuthenticatedCaller`, which only exists after a token
has been validated (FR-007).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from pydantic import BaseModel, ConfigDict, Field

from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan
from groundwork_contracts.readiness import ReadinessSummary
from groundwork_contracts.tenant import CustomerTenant
from groundwork_controlplane.agents.planning import PlanningAgent
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthorizationError, TokenValidator
from groundwork_controlplane.api.errors import FailedAssertion, PlanNotDeployableError
from groundwork_controlplane.approval.plan_identity import seal_plan
from groundwork_controlplane.costing.licensing import licensing_disclosure_for
from groundwork_controlplane.validation.engine import ReadinessEngine
from groundwork_controlplane.validation.registry import build_validation_context
from groundwork_orchestrator.state.repositories import CustomerTenantRepository, PlanRepository

router = APIRouter(prefix="/v1", tags=["plans"])


def get_authenticated_caller(request: Request) -> AuthenticatedCaller:
    """FR-005/FR-007: every route in this module requires a validated token.

    A plain function, not a class, because there is nothing to configure per call site — the
    validator and its policy are built once at startup (``main.py``'s lifespan) and reused.
    """
    validator: TokenValidator = request.app.state.token_validator
    return validator.validate(request.headers.get("Authorization"))


def get_planning_agent(blueprint_id: str, request: Request | WebSocket) -> PlanningAgent:
    planning_agents: dict[str, PlanningAgent] = getattr(request.app.state, "planning_agents", {})
    planning_agent = planning_agents.get(blueprint_id)
    if planning_agent is None:
        planning_agent = getattr(request.app.state, "planning_agent", None)
    if planning_agent is None:
        raise HTTPException(
            status_code=500,
            detail=f"planning agent for blueprint {blueprint_id!r} is not available",
        )
    return planning_agent


def get_readiness_engine(blueprint_id: str, request: Request | WebSocket) -> ReadinessEngine:
    readiness_engines: dict[str, ReadinessEngine] = getattr(
        request.app.state, "readiness_engines", {}
    )
    engine = readiness_engines.get(blueprint_id)
    if engine is None:
        engine = getattr(request.app.state, "readiness_engine", None)
    if engine is None:
        raise HTTPException(
            status_code=500,
            detail=f"readiness engine for blueprint {blueprint_id!r} is not available",
        )
    return engine


def get_plan_repository(request: Request) -> PlanRepository:
    repository: PlanRepository = request.app.state.plan_repository
    return repository


def get_blueprint(blueprint_id: str, request: Request | WebSocket) -> PlatformBlueprint:
    catalogue: dict[str, PlatformBlueprint] = getattr(request.app.state, "blueprints", {})
    blueprint = catalogue.get(blueprint_id)
    if blueprint is None:
        raise HTTPException(
            status_code=400,
            detail=f"blueprint {blueprint_id!r} is not in the approved catalogue",
        )
    return blueprint


async def get_customer_tenant(request: Request | WebSocket, tenant_id: str) -> CustomerTenant:
    repository: CustomerTenantRepository = request.app.state.tenant_repository
    tenant = await repository.read(tenant_id, tenant_id)
    if tenant is None:
        # No onboarding record exists for this tenant yet. Structurally identical to "not
        # entitled" from the caller's point of view — FR-008 gives no distinction between "unknown
        # tenant" and "known tenant, no grant for this subscription", and inventing one would leak
        # which tenants Groundwork has heard of to an unauthenticated-in-practice caller.
        raise AuthorizationError("no onboarding record exists for this tenant")
    if not tenant.consent_state.permits_tenant_operations:
        # FR-006: consent to the multi-tenant application is the basis of *all* authority in a
        # customer tenant, not just the subscription-level entitlement checked separately below.
        # PENDING and REVOKED both deny access — see tenant.py's own module docstring.
        raise AuthorizationError(
            f"tenant consent state is {tenant.consent_state.value!r}, not granted"
        )
    return tenant


class CreatePlanRequest(BaseModel):
    """The caller-supplied subset of ``deployment-plan.schema.json`` (T049; FR-033).

    ``extra="forbid"`` is the structural half of "tenantId is not accepted in the body" (FR-007): a
    caller who includes it gets a 400, not a silently ignored field.
    """

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    blueprint_id: Annotated[str, Field(alias="blueprintId")]
    blueprint_version: Annotated[str, Field(alias="blueprintVersion")]
    subscription_id: Annotated[str, Field(alias="subscriptionId")]
    region: Annotated[str, Field(alias="region")]
    environment: Annotated[str, Field(alias="environment")] = "production"
    fabric_capacity_sku: Annotated[str | None, Field(alias="fabricCapacitySku")] = None

    def conversation_summary(self, blueprint: PlatformBlueprint) -> str:
        """Render this structured request as the prompt text the planning agent expects.

        The non-conversational path still goes through the same agent as a Teams turn would
        (module docstring) — this is the one adapter that lets a structured request stand in for
        the conversational summary a chat turn would otherwise produce.
        """
        sku_clause = (
            f"The customer has explicitly requested Fabric capacity SKU {self.fabric_capacity_sku}."
            if self.fabric_capacity_sku
            else "The customer has not specified a Fabric capacity SKU; apply your default rule."
        )
        return (
            f"Deploy the {blueprint.display_name!r} blueprint "
            f"(blueprint_id={blueprint.blueprint_id}, version={blueprint.version}) into "
            f"subscription {self.subscription_id}, region {self.region}, "
            f"environment {self.environment}. {sku_clause}"
        )


async def _evaluate_readiness(
    request: Request, *, tenant_id: str, subscription_id: str, region: str, blueprint_id: str
) -> ReadinessSummary:
    """Run the same :class:`ReadinessEngine` ``POST /validate`` uses (FR-015).

    Called from ``create_plan`` and ``get_plan`` too, because the contract's response shape
    (``validationSummary``, ``deployable``) is "current", not "as of creation" — a plan is never
    presented as deployable on stale information, the same principle FR-017 states for the
    dedicated validate route.
    """
    engine = get_readiness_engine(blueprint_id, request)
    # Tenant-record-first resolution of the customer's own DevOps organization URL (the same
    # rule the orchestrator's queue loop applies at execution time), with the worker-wide
    # setting as fallback — one read here covers every readiness check in the contract.
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    context = build_validation_context(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        region=region,
        credential=request.app.state.credential,
        readiness=request.app.state.settings.readiness,
        blueprint=get_blueprint(blueprint_id, request),
        tenant_devops_organization_url=(
            tenant.devops_organization_url if tenant is not None else None
        ),
    )
    return await engine.evaluate(context, now=datetime.now(UTC))


def _plan_response(sealed: SealedDeploymentPlan, *, summary: ReadinessSummary) -> dict[str, object]:
    plan = sealed.plan
    body: dict[str, object] = {
        "planId": sealed.plan_hash,
        "planHash": sealed.plan_hash,
        "blueprintId": plan.blueprint_id,
        "blueprintVersion": plan.blueprint_version,
        "subscriptionId": plan.subscription_id,
        "region": plan.region.value,
        "environment": plan.environment.value,
        "fabricCapacitySku": plan.fabric_capacity_sku.value,
        "resourceSet": [
            {"resourceType": r.resource_type, "logicalName": r.logical_name}
            for r in plan.resource_set
        ],
        "estimatedDurationMinutes": plan.estimated_duration_minutes,
        "costEstimate": {
            "currency": plan.cost_estimate.currency,
            "monthlyTotal": plan.cost_estimate.monthly_total,
            "uncertaintyLowerPct": plan.cost_estimate.uncertainty_lower_pct,
            "uncertaintyUpperPct": plan.cost_estimate.uncertainty_upper_pct,
            "basis": plan.cost_estimate.basis,
        },
        # FR-013d: the caveat below F64 must be shown at plan time, not discovered at approval
        # time - the boolean flags it, the statement is the disclosure itself, and the approval
        # gate (approval/service.py) requires the caller to acknowledge having been shown it
        # (acknowledgedPowerBiViewerLicensing) before any approval record can exist.
        "requiresPowerBiViewerLicensing": plan.requires_licensing_disclosure(),
        "licensingDisclosure": licensing_disclosure_for(plan.fabric_capacity_sku),
        "riskAssessment": {
            "severity": plan.risk_assessment.severity.value,
            "findings": [
                {"description": f.description, "impact": f.impact}
                for f in plan.risk_assessment.findings
            ],
        },
        "validityWindow": {
            "notBefore": sealed.validity.not_before.isoformat(),
            "notAfter": sealed.validity.not_after.isoformat(),
        },
        "deployable": summary.deployable,
        "validationSummary": {
            "contractVersion": summary.contract_version,
            "evaluatedAt": summary.evaluated_at.isoformat(),
            "results": [
                {
                    "assertionId": r.assertion_id,
                    "designArea": r.design_area.value,
                    "status": r.status.value,
                    "finding": r.finding,
                    "remediation": r.remediation,
                }
                for r in summary.results
            ],
        },
    }
    return body


@router.post("/plans", status_code=201)
async def create_plan(
    body: CreatePlanRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    plan_repository: Annotated[PlanRepository, Depends(get_plan_repository)],
) -> dict[str, object]:
    """``POST /plans`` — create a deployment plan from structured parameters (FR-033)."""
    tenant = await get_customer_tenant(request, caller.tenant_id)

    if tenant.entitlement_for(body.subscription_id) is None:
        raise AuthorizationError(f"tenant is not entitled to subscription {body.subscription_id}")
    if body.region not in tenant.approved_regions:
        raise HTTPException(
            status_code=409,
            detail=f"region {body.region!r} is outside this tenant's approved region set",
        )

    blueprint = get_blueprint(body.blueprint_id, request)
    planning_agent = get_planning_agent(blueprint.blueprint_id, request)
    if blueprint.version != body.blueprint_version:
        raise HTTPException(
            status_code=400,
            detail=(
                f"blueprint {body.blueprint_id!r} is at version {blueprint.version!r}, "
                f"not {body.blueprint_version!r}"
            ),
        )

    plan: DeploymentPlan = await planning_agent.generate_plan(body.conversation_summary(blueprint))
    if plan.blueprint_id != blueprint.blueprint_id or plan.blueprint_version != blueprint.version:
        raise HTTPException(
            status_code=500,
            detail=(
                "planning agent returned a plan for "
                f"{plan.blueprint_id!r} version {plan.blueprint_version!r}, expected "
                f"{blueprint.blueprint_id!r} version {blueprint.version!r}"
            ),
        )

    sealed = seal_plan(
        plan,
        tenant_id=caller.tenant_id,
        requesting_identity_object_id=caller.object_id,
        requesting_channel="api",
    )
    created = await plan_repository.create(caller.tenant_id, sealed)

    summary = await _evaluate_readiness(
        request,
        tenant_id=caller.tenant_id,
        subscription_id=created.plan.subscription_id,
        region=created.plan.region.value,
        blueprint_id=created.plan.blueprint_id,
    )
    return _plan_response(created, summary=summary)


@router.get("/plans/{plan_id}")
async def get_plan(
    plan_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    plan_repository: Annotated[PlanRepository, Depends(get_plan_repository)],
) -> dict[str, object]:
    """``GET /plans/{planId}`` — never a cross-tenant read (FR-032)."""
    sealed = await plan_repository.read(caller.tenant_id, plan_id)
    if sealed is None:
        raise HTTPException(status_code=404, detail="plan not found")

    summary = await _evaluate_readiness(
        request,
        tenant_id=caller.tenant_id,
        subscription_id=sealed.plan.subscription_id,
        region=sealed.plan.region.value,
        blueprint_id=sealed.plan.blueprint_id,
    )
    return _plan_response(sealed, summary=summary)


@router.post("/plans/{plan_id}/validate")
async def validate_plan(
    plan_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
    plan_repository: Annotated[PlanRepository, Depends(get_plan_repository)],
) -> list[dict[str, object]]:
    """``POST /plans/{planId}/validate`` — re-runs readiness against the real target (FR-015)."""
    sealed = await plan_repository.read(caller.tenant_id, plan_id)
    if sealed is None:
        raise HTTPException(status_code=404, detail="plan not found")

    summary = await _evaluate_readiness(
        request,
        tenant_id=caller.tenant_id,
        subscription_id=sealed.plan.subscription_id,
        region=sealed.plan.region.value,
        blueprint_id=sealed.plan.blueprint_id,
    )

    if not summary.deployable:
        raise PlanNotDeployableError(
            tuple(
                FailedAssertion(
                    assertion_id=r.assertion_id,
                    design_area=r.design_area.value,
                    finding=r.finding,
                    remediation=r.remediation,
                )
                for r in summary.blocking_failures
            )
        )

    return [
        {
            "assertionId": r.assertion_id,
            "designArea": r.design_area.value,
            "status": r.status.value,
            "finding": r.finding,
            "remediation": r.remediation,
        }
        for r in summary.results
    ]
