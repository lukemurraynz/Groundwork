"""Approval binding (T065-T066) — the gated-approval rule.

Validates exactly what ``control-plane-api.md``'s ``POST /plans/{planId}/approvals`` route
promises to check — plan hash, acknowledged cost, validity window — as explicit, named failures
raised *before* any :class:`~groundwork_contracts.approval.Approval` or
:class:`~groundwork_contracts.approval.PendingApproval` is constructed. The contracts models'
own validators remain the authoritative final check (nothing here duplicates their logic, e.g.
the second-approver-distinctness or licensing-disclosure rules) — these explicit checks exist so a
caller gets a specific, named exception mapped to the right HTTP status, rather than a generic
``pydantic.ValidationError`` whose status code would have to be guessed from its message.

As of 2026-08-02 (see ADR-0011), channel durability is no longer checked — voice alone may
authorise an irreversible action. The ``NonDurableApprovalChannelError`` exception class is kept
for backward compatibility with any caller that still references it, but it is no longer raised
from this module.

Cost is re-verified live at approval time, not read from the plan's own (already possibly stale)
``cost_estimate`` — the same "never trust a cached figure" discipline
``agents/planning.py``'s module docstring applies to plan creation, applied here to approval.

Optional step-up enforcement is configuration-only: when enabled, approval requires validated token
evidence of MFA (``amr`` contains ``mfa``) or a freshly issued token (``iat`` within 10 minutes).
Entra Conditional Access configures and enforces the actual MFA prompt; this module only refuses to
approve when the token does not prove that stronger sign-in happened.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from groundwork_contracts.approval import (
    ActorKind,
    Approval,
    ApprovalChannel,
    ApprovedParameters,
    ApprovingIdentity,
    CostEstimate,
    PendingApproval,
    SecondApproval,
    ThresholdPolicy,
)
from groundwork_contracts.plan import SealedDeploymentPlan
from groundwork_controlplane.api.auth import AuthenticatedCaller
from groundwork_controlplane.approval.artefacts import ApprovalArtefactStore
from groundwork_controlplane.approval.plan_identity import is_within_validity_window
from groundwork_shared.costing.estimator import compose_estimate, fabric_capacity_line
from groundwork_shared.costing.retail_prices import RetailPricesClient


class PlanHashMismatchError(Exception):
    """The caller's ``planHash`` does not match the server's recomputed hash (FR-022)."""


class PlanExpiredError(Exception):
    """The plan's validity window has passed (FR-012, FR-025)."""


class NonDurableApprovalChannelError(Exception):
    """Kept for backward compatibility — no longer raised (see ADR-0011, 2026-08-02).
    Voice may now authorise an irreversible action on its own."""


class CostAcknowledgementMismatchError(Exception):
    """``acknowledgedCostAud`` falls outside the live-recomputed cost estimate's band (FR-019)."""


class DuplicateApprovalError(Exception):
    """A second approval was attempted by the same identity, or the plan is already fully
    approved (SC-018)."""


class LicensingDisclosureNotAcknowledgedError(Exception):
    """FR-013d: a below-F64 plan was submitted for approval without the caller acknowledging
    the Power BI viewer-licensing disclosure. The plan response carries the disclosure text;
    an approval that never saw it acknowledged must not exist."""


class NotificationEmailMissingError(Exception):
    """The tenant has no recorded notification recipient at approval time.

    Approving a plan makes a deployment possible, and a deployment that ends with an outcome
    the customer cannot be told about is a silently-failed handover. The notifier would skip
    delivery when ``CustomerTenant.notification_email`` is unset, which is why approval refuses
    instead: the email must be recorded (POST /v1/tenants/{tenantId}/notification-email, or the
    conversational capture during planning) before an approval may be recorded.
    """


class StepUpAuthenticationRequiredError(Exception):
    """Approval requires fresh or MFA-backed authentication evidence in the caller token.

    Entra Conditional Access enforces the real MFA challenge; this control-plane check only refuses
    approval when the validated token lacks evidence of that stronger authentication.
    """


_STEP_UP_MAX_TOKEN_AGE = timedelta(minutes=10)


def step_up_authentication_satisfied(caller: AuthenticatedCaller, *, now: datetime) -> bool:
    """Whether ``caller``'s current token already carries step-up evidence (MFA in ``amr``, or
    issued within the last 10 minutes) — the same rule :func:`_require_step_up_authentication`
    enforces, exposed as a plain predicate so a caller can check *before* attempting an approval,
    not only discover the gap from a 403 on the approval call itself (customer-journey-map.md
    Quick Win, 2026-09-13: pre-check token freshness/MFA before submission)."""
    if "mfa" in caller.authentication_methods:
        return True
    issued_at = caller.token_issued_at
    return issued_at is not None and issued_at <= now and now - issued_at <= _STEP_UP_MAX_TOKEN_AGE


def _require_step_up_authentication(caller: AuthenticatedCaller, *, now: datetime) -> None:
    if step_up_authentication_satisfied(caller, now=now):
        return
    raise StepUpAuthenticationRequiredError(
        "approval requires a token that shows MFA in the amr claim or was issued within the last "
        "10 minutes"
    )


async def record_approval(
    *,
    sealed_plan: SealedDeploymentPlan,
    existing: Approval | PendingApproval | None,
    caller: AuthenticatedCaller,
    plan_hash: str,
    acknowledged_cost_aud: float,
    channel: ApprovalChannel,
    threshold: ThresholdPolicy,
    retail_prices_client: RetailPricesClient,
    artefact_store: ApprovalArtefactStore,
    now: datetime,
    acknowledged_powerbi_viewer_licensing: bool = False,
    require_step_up_approval: bool = False,
    notification_email: str | None = None,
) -> Approval | PendingApproval:
    """Record an approval or a second approval, returning whichever results.

    Args:
        existing: ``None`` for a first approval, a :class:`PendingApproval` if a first approval
            above threshold is awaiting a second, or a complete :class:`Approval` if this plan
            already has one (any further call is a duplicate).
        acknowledged_powerbi_viewer_licensing: the caller's explicit acknowledgement of
            FR-013d's Power BI viewer-licensing disclosure. Required (``True``) when the plan's
            SKU is below F64; the same shape as ``acknowledged_cost_aud`` because it is the
            same class of guarantee — the caller confirms they were shown something material
            before authorising it.
        notification_email: the tenant's recorded notification recipient
            (``CustomerTenant.notification_email``). Required: approving without it guarantees
            the outcome notification will be silently skipped, so approval refuses instead.
    """
    if plan_hash != sealed_plan.plan_hash:
        raise PlanHashMismatchError(
            "submitted planHash does not match the current plan; the plan changed after it was "
            "reviewed and must be re-approved (FR-022)"
        )
    if not is_within_validity_window(sealed_plan, now=now):
        raise PlanExpiredError(
            f"plan validity window expired at {sealed_plan.validity.not_after.isoformat()}; "
            f"FR-025 requires an expired plan to be refused, not silently re-validated"
        )
    if require_step_up_approval:
        _require_step_up_authentication(caller, now=now)
    if sealed_plan.plan.requires_licensing_disclosure() and not (
        acknowledged_powerbi_viewer_licensing
    ):
        raise LicensingDisclosureNotAcknowledgedError(
            f"SKU {sealed_plan.plan.fabric_capacity_sku.value} is below F64, so every user "
            f"viewing Power BI content will need a Pro or PPU licence; FR-013d requires the "
            f"disclosure to be acknowledged (acknowledgedPowerBiViewerLicensing) before this "
            f"plan can be approved"
        )
    if notification_email is None:
        raise NotificationEmailMissingError(
            "the tenant has no recorded notification_email; record it via "
            "POST /v1/tenants/{tenantId}/notification-email (or the conversational capture "
            "during planning) before approving — without it the deployment-outcome "
            "notification would be silently skipped"
        )

    fabric_line = await fabric_capacity_line(
        retail_prices_client, sealed_plan.plan.fabric_capacity_sku, sealed_plan.plan.region.value
    )
    # FR-013d: the acknowledgement gate above has passed by this point (or did not apply), so
    # recording the estimate as disclosed is a fact about what this approver acknowledged, not
    # an assertion about what some earlier response carried.
    live_estimate = compose_estimate(fabric_line, now=now).model_copy(
        update={"powerbi_viewer_licensing_disclosed": True}
    )
    if not is_within_validity_window(sealed_plan, now=now):
        raise PlanExpiredError(
            f"plan validity window expired at {sealed_plan.validity.not_after.isoformat()}; "
            f"FR-025 requires an expired plan to be refused, not silently re-validated"
        )

    fabric_line = await fabric_capacity_line(
        retail_prices_client, sealed_plan.plan.fabric_capacity_sku, sealed_plan.plan.region.value
    )
    # FR-013d: the plan response already discloses requiresPowerBiViewerLicensing before an
    # approver could have reached this route — see api/plans.py's _plan_response.
    live_estimate = compose_estimate(fabric_line, now=now).model_copy(
        update={"powerbi_viewer_licensing_disclosed": True}
    )
    if not live_estimate.contains(acknowledged_cost_aud):
        raise CostAcknowledgementMismatchError(
            f"acknowledged cost {acknowledged_cost_aud} AUD is outside the current estimate's "
            f"band ({live_estimate.monthly_total} AUD ± "
            f"{live_estimate.uncertainty_lower_pct}/{live_estimate.uncertainty_upper_pct}%); "
            f"FR-019 requires the caller to acknowledge a figure they were actually shown"
        )

    approved_parameters = ApprovedParameters(
        tenant_id=sealed_plan.tenant_id,
        subscription_id=sealed_plan.plan.subscription_id,
        region=sealed_plan.plan.region.value,
        fabric_capacity_sku=sealed_plan.plan.fabric_capacity_sku.value,
        environment=sealed_plan.plan.environment.value,
        monthly_total_aud=live_estimate.monthly_total,
    )
    approving_identity = ApprovingIdentity(
        object_id=caller.object_id, display_name=caller.display_name, actor_kind=ActorKind.HUMAN
    )

    if existing is None:
        return await _record_first_approval(
            approval_id=str(uuid.uuid4()),
            tenant_id=caller.tenant_id,
            plan_hash=plan_hash,
            approving_identity=approving_identity,
            channel=channel,
            approved_parameters=approved_parameters,
            cost_estimate=live_estimate,
            threshold=threshold,
            artefact_store=artefact_store,
            now=now,
        )

    if isinstance(existing, Approval):
        raise DuplicateApprovalError(
            "this plan already has a complete approval; a repeat approval is not accepted"
        )

    # existing is a PendingApproval: this is the second, distinct approver.
    if existing.approving_identity.object_id == caller.object_id:
        raise DuplicateApprovalError(
            "second approver must be a different identity from the first (FR-020a, SC-018)"
        )
    second = SecondApproval(
        approving_identity=approving_identity,
        approved_at=now,
        channel=channel,
        artefact_uri=existing.artefact_uri,
    )
    completed = existing.complete(second)
    await artefact_store.store(
        approval_id=completed.approval_id, payload=completed.model_dump(mode="json")
    )
    return completed


async def _record_first_approval(
    *,
    approval_id: str,
    tenant_id: str,
    plan_hash: str,
    approving_identity: ApprovingIdentity,
    channel: ApprovalChannel,
    approved_parameters: ApprovedParameters,
    cost_estimate: CostEstimate,
    threshold: ThresholdPolicy,
    artefact_store: ApprovalArtefactStore,
    now: datetime,
) -> Approval | PendingApproval:
    artefact_uri = artefact_store.blob_url_for(approval_id)
    needs_second = threshold.requires_second_approver(
        approved_parameters.monthly_total_aud, approved_parameters.environment
    )

    record: Approval | PendingApproval
    if needs_second:
        record = PendingApproval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            plan_hash=plan_hash,
            approving_identity=approving_identity,
            approved_at=now,
            channel=channel,
            artefact_uri=artefact_uri,
            approved_parameters=approved_parameters,
            cost_estimate=cost_estimate,
            threshold_applied=threshold,
        )
    else:
        record = Approval(
            approval_id=approval_id,
            tenant_id=tenant_id,
            plan_hash=plan_hash,
            approving_identity=approving_identity,
            approved_at=now,
            channel=channel,
            artefact_uri=artefact_uri,
            approved_parameters=approved_parameters,
            cost_estimate=cost_estimate,
            threshold_applied=threshold,
            second_approval=None,
        )

    await artefact_store.store(approval_id=approval_id, payload=record.model_dump(mode="json"))
    return record
