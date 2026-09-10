"""``POST /plans/{planId}/approvals`` (T067) — the gated-approval rule.

Every check ``control-plane-api.md`` documents for this route lives in
``groundwork_controlplane.approval.service.record_approval``; this module is the HTTP adapter
around it — request parsing, looking up what (if anything) already exists for this plan, and
shaping the response, including the ``secondApprovalRequired`` flag the contract requires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from azure.core.credentials_async import AsyncTokenCredential
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from groundwork_contracts.approval import (
    Approval,
    ApprovalChannel,
    PendingApproval,
    ThresholdPolicy,
)
from groundwork_controlplane.api.auth import AuthenticatedCaller, CallerRole
from groundwork_controlplane.api.plans import get_authenticated_caller
from groundwork_controlplane.approval.lookup import find_approval_by_plan_hash
from groundwork_controlplane.approval.service import record_approval
from groundwork_shared.storage.sas import read_only_sas_url

router = APIRouter(prefix="/v1", tags=["approvals"])


class CreateApprovalRequest(BaseModel):
    """``{ planHash, acknowledgedCostAud, channel }`` — exactly the caller-supplied subset
    ``control-plane-api.md`` documents. No ``tenantId``: authority derives from the token (FR-007),
    the same rule ``plans.py``'s ``CreatePlanRequest`` enforces."""

    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    plan_hash: Annotated[str, Field(alias="planHash", pattern=r"^sha256:[0-9a-f]{64}$")]
    acknowledged_cost_aud: Annotated[float, Field(alias="acknowledgedCostAud", ge=0)]
    channel: Annotated[ApprovalChannel, Field(alias="channel")]
    acknowledged_powerbi_viewer_licensing: Annotated[
        bool, Field(alias="acknowledgedPowerBiViewerLicensing")
    ] = False
    """FR-013d: required ``true`` when the plan's SKU is below F64. The plan response carried
    the disclosure text (``licensingDisclosure``); the approval records that the approver
    acknowledged it - same shape as ``acknowledgedCostAud``."""


def _threshold_policy(request: Request) -> ThresholdPolicy:
    governance = request.app.state.settings.governance
    return ThresholdPolicy(
        monthly_amount_aud=governance.approval_threshold_aud,
        approver_role=governance.approver_role,
        applies_to_environments=("production",),
    )


def _now(request: Request) -> datetime:
    """Injectable via ``app.state.now_fn`` — defaults to the real clock when absent, same
    discipline as the orchestrator's ``Sequencer._Clock``. Without this, a validity-window check
    against a hardcoded ``datetime.now(UTC)`` call is a real clock dependency baked into every
    caller, including tests that seal a plan against a fixed past ``now`` — exactly the failure
    mode this codebase's own ``tests/conftest.py`` docstring warns against ("a time-dependent test
    that passes today and fails at midnight is worse than no test"), and which this route's tests
    hit for real once enough wall-clock time had actually passed."""
    now_fn = getattr(request.app.state, "now_fn", None)
    return now_fn() if now_fn is not None else datetime.now(UTC)


async def _approval_response(
    record: Approval | PendingApproval, *, credential: AsyncTokenCredential
) -> dict[str, object]:
    # WAF assessment §2.7: mint a fresh, time-limited SAS at response time rather than echoing
    # the permanent blob URL the approval record stores — same fix, same reasoning as
    # api/reports.py's blobUri.
    artefact_uri = await read_only_sas_url(record.artefact_uri, credential=credential)
    body: dict[str, object] = {
        "approvalId": record.approval_id,
        "planHash": record.plan_hash,
        "approvingIdentity": {
            "objectId": record.approving_identity.object_id,
            "displayName": record.approving_identity.display_name,
        },
        "approvedAt": record.approved_at.isoformat(),
        "channel": record.channel.value,
        "artefactUri": artefact_uri,
        "acknowledgedCostAud": record.approved_parameters.monthly_total_aud,
        "thresholdAppliedAud": record.threshold_applied.monthly_amount_aud,
    }
    if isinstance(record, PendingApproval):
        body["secondApprovalRequired"] = True
    else:
        body["secondApprovalRequired"] = False
        body["isSelfApproval"] = record.is_self_approval
    return body


@router.post("/plans/{plan_id}/approvals", status_code=201)
async def create_approval(
    plan_id: str,
    body: CreateApprovalRequest,
    request: Request,
    caller: Annotated[AuthenticatedCaller, Depends(get_authenticated_caller)],
) -> dict[str, object]:
    caller.require_role(CallerRole.APPROVER)

    plan_repository = request.app.state.plan_repository
    sealed = await plan_repository.read(caller.tenant_id, plan_id)
    if sealed is None:
        raise HTTPException(status_code=404, detail="plan not found")

    existing = await find_approval_by_plan_hash(
        pending_repository=request.app.state.pending_approval_repository,
        approval_repository=request.app.state.approval_repository,
        tenant_id=caller.tenant_id,
        plan_hash=sealed.plan_hash,
    )

    now = _now(request)
    record = await record_approval(
        sealed_plan=sealed,
        existing=existing,
        caller=caller,
        plan_hash=body.plan_hash,
        acknowledged_cost_aud=body.acknowledged_cost_aud,
        channel=body.channel,
        threshold=_threshold_policy(request),
        retail_prices_client=request.app.state.retail_prices_client,
        artefact_store=request.app.state.approval_artefact_store,
        now=now,
        acknowledged_powerbi_viewer_licensing=body.acknowledged_powerbi_viewer_licensing,
        require_step_up_approval=request.app.state.settings.governance.require_step_up_approval,
    )

    if isinstance(record, PendingApproval):
        await request.app.state.pending_approval_repository.replace(caller.tenant_id, record)
    else:
        await request.app.state.approval_repository.replace(caller.tenant_id, record)

    return await _approval_response(record, credential=request.app.state.credential)
