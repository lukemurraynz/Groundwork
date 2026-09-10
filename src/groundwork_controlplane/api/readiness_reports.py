"""Operator-facing readiness report retrieval.

Returns either minimal JSON metadata or an accessible HTML rendering of the latest stored
readiness assessment for the tenant's first entitled subscription.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse

import groundwork_controlplane.api.errors as api_errors
from groundwork_contracts.tenant import CustomerTenant
from groundwork_controlplane.api.auth import AuthenticatedCaller, CallerRole
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_controlplane.api.reports import _now, _wants_html
from groundwork_shared.validation.report import render_readiness_html

router = APIRouter(prefix="/v1/tenants", tags=["tenants"])


def _first_entitled_subscription_id(tenant: CustomerTenant) -> str:
    for entitlement in tenant.subscriptions:
        if entitlement.may_deploy:
            return entitlement.subscription_id
    if len(tenant.subscriptions) == 1:
        return tenant.subscriptions[0].subscription_id
    raise api_errors.ReadinessReportNotFoundError(
        "readiness report not yet generated for any entitled subscription"
    )


@router.get("/{tenant_id}/onboarding/readiness-report", response_model=None)
async def get_readiness_report(
    tenant_id: str,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object] | HTMLResponse:
    caller.require_role(CallerRole.OPERATOR)
    tenant_repository = request.app.state.tenant_repository
    tenant = await tenant_repository.read(tenant_id, tenant_id)
    if tenant is None:
        raise api_errors.TenantNotFoundError()

    subscription_id = _first_entitled_subscription_id(tenant)
    drift_summary = await request.app.state.drift_summary_repository.read(
        tenant_id, subscription_id
    )
    if drift_summary is None:
        raise api_errors.ReadinessReportNotFoundError("readiness report not yet generated")

    if _wants_html(request):
        return HTMLResponse(
            content=render_readiness_html(
                tenant_id=tenant_id,
                subscription_id=subscription_id,
                summary=drift_summary.summary,
                generated_at=_now(request),
            )
        )

    return {
        "verdict": drift_summary.verdict.value,
        "evaluatedAt": drift_summary.summary.evaluated_at.isoformat(),
        "blockingCount": len(drift_summary.summary.blocking_failures),
    }
