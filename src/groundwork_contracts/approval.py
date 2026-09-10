"""Approval, cost estimate, and threshold policy.

The gated-approval rule: no write to a customer tenant without an approval bound to a specific
plan identity, and an agent never self-approves.

As of 2026-08-02 (see ADR-0011), voice alone MAY authorise an irreversible action at any
cost/threshold — the prior FR-023 prohibition is removed by explicit product-owner decision.
:attr:`ApprovalChannel.yields_durable_artefact` remains informational but no longer gates anything.
The distinct-identity requirement for a second approver is unaffected (SC-018, enforced by
:meth:`Approval._second_approver_present_and_distinct`).

The structural decisions here:

- :class:`Approval` requires ``plan_hash``. There is no constructor that produces an approval
  without one, so an approval can never float free of the thing it approved.
- Where the threshold is exceeded, :class:`Approval` validates that a second approver exists and
  is a *different* identity. Same-identity double approval is a hard reject (SC-018), not a warning.
- ``approving_actor_type`` exists so that an agent-authored approval is representable and therefore
  rejectable. Making it unrepresentable would seem safer but would mean an attempted agent approval
  fails as a generic schema error instead of a named gated-approval-rule violation.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


class ApprovalChannel(StrEnum):
    """Channels through which an approval may be recorded.

    ``VOICE`` is Groundwork's primary entry point. As of 2026-08-02 (see ADR-0011), voice
    alone MAY authorise an irreversible action — this reverses the prior FR-023 prohibition and is
    a deliberate, accepted-risk product-owner decision. The distinct-identity requirement for a
    second approver is unaffected: two confirmations from the same identity, by any channel, still
    fail. The open gap is session strength, not caller identity — voice ships as the authenticated
    web frontend only (no PSTN path is wired), so every voice session already presents a validated
    Entra token; what remains unresolved is whether a spoken utterance from that authenticated
    session should be enough to approve on its own. ``require_step_up_approval`` (see
    ``groundwork_controlplane.approval.service``) is the existing, tested control for exactly that
    question; it defaults on as of 2026-09-06. See ADR-0011 for the full history.

    :attr:`yields_durable_artefact` is informational, not a gate — voice does not yield a durable
    artefact, but that no longer blocks its use.
    """

    TEAMS = "teams"
    PORTAL = "portal"
    API = "api"
    EMAIL = "email"
    VOICE = "voice"

    @property
    def yields_durable_artefact(self) -> bool:
        """Whether this channel produces a durable attributable record.

        Informational, not a gate — ADR-0011 removed the requirement that approval
        MUST come through a durable channel. Voice returns ``False`` but is fully accepted.
        """
        return self is not ApprovalChannel.VOICE


class ActorKind(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    WORKLOAD = "workload"


class CostComponentKind(StrEnum):
    FABRIC_CAPACITY = "fabric-capacity"
    STORAGE = "storage"
    NETWORKING = "networking"
    MONITORING = "monitoring"
    OTHER = "other"


class CostComponent(BaseModel):
    model_config = StrictModel

    component: CostComponentKind
    monthly_amount: Annotated[float, Field(ge=0)]
    note: str | None = None


class CostEstimate(BaseModel):
    """Monthly cost with a mandatory uncertainty band (FR-018).

    The band is what FR-019 compares against at execution time: a re-checked figure inside the band
    was already accepted, so re-prompting the approver would fire on pricing noise and train people
    to click through approvals.
    """

    model_config = StrictModel

    currency: Literal["AUD"] = "AUD"
    components: Annotated[tuple[CostComponent, ...], Field(min_length=1)]
    monthly_total: Annotated[float, Field(ge=0)]
    uncertainty_lower_pct: Annotated[float, Field(ge=0, le=100)]
    uncertainty_upper_pct: Annotated[float, Field(ge=0, le=100)]
    basis: Annotated[str, Field(min_length=1)]
    computed_at: datetime
    billed_to: Literal["customer"] = "customer"
    powerbi_viewer_licensing_disclosed: bool = False
    """FR-013d: below F64, Power BI viewers each need Pro or PPU.

    The plan must not reach approval with this False when the SKU is below F64. Enforced at the
    approval boundary rather than here, because this model does not know the SKU.
    """

    @model_validator(mode="after")
    def _total_matches_components(self) -> Self:
        component_sum = sum(c.monthly_amount for c in self.components)
        # Tolerance for float accumulation only, not for genuine disagreement. A total that does not
        # reflect its own line items would mislead the approver about what they are agreeing to.
        if abs(component_sum - self.monthly_total) > 0.01:
            raise ValueError(
                f"monthly_total {self.monthly_total} does not match the sum of components "
                f"{component_sum:.2f}; an approver must be able to reconcile what they approve"
            )
        return self

    @model_validator(mode="after")
    def _band_must_be_meaningful(self) -> Self:
        if self.uncertainty_lower_pct == 0 and self.uncertainty_upper_pct == 0:
            raise ValueError(
                "zero uncertainty band claims perfect precision about future cloud spend; FR-018 "
                "requires the estimate to state its uncertainty"
            )
        return self

    def band_absolute(self) -> tuple[float, float]:
        lower = self.monthly_total * (1 - self.uncertainty_lower_pct / 100)
        upper = self.monthly_total * (1 + self.uncertainty_upper_pct / 100)
        return lower, upper

    def contains(self, amount: float) -> bool:
        """Whether ``amount`` is inside the approved band — the FR-019 re-approval test."""
        lower, upper = self.band_absolute()
        return lower <= amount <= upper


class ThresholdPolicy(BaseModel):
    """When a second approver is required (FR-020a).

    Configuration, never conversation-adjustable. The control plane reads this from tenant config;
    nothing in the request body or the transcript can alter it.
    """

    model_config = StrictModel

    monthly_amount_aud: Annotated[float, Field(ge=0)]
    approver_role: Annotated[str, Field(min_length=1)]
    applies_to_environments: tuple[str, ...] = ("production",)

    def requires_second_approver(self, monthly_total: float, environment: str) -> bool:
        if environment not in self.applies_to_environments:
            return False
        return monthly_total >= self.monthly_amount_aud


class ApprovingIdentity(BaseModel):
    model_config = StrictModel

    object_id: Annotated[str, Field(pattern=_GUID)]
    display_name: Annotated[str, Field(min_length=1)]
    actor_kind: ActorKind

    @model_validator(mode="after")
    def _agents_may_not_approve(self) -> Self:
        # The gated-approval rule: an agent MUST NOT self-approve. Rejected by name so the failure
        # is attributable to the rule rather than surfacing as a generic validation error.
        if self.actor_kind is ActorKind.AGENT:
            raise ValueError(
                "an agent may not approve a plan (the gated-approval rule, FR-021). Approval "
                "must come from a human identity."
            )
        return self


class SecondApproval(BaseModel):
    """A second, distinct approver's confirmation (FR-020a).

    Any channel is accepted, including voice (see ADR-0011, 2026-08-02).
    The distinct-identity rule is enforced by :class:`Approval`, not here.
    """

    model_config = StrictModel

    approving_identity: ApprovingIdentity
    approved_at: datetime
    channel: ApprovalChannel
    artefact_uri: Annotated[str, Field(min_length=1)]


class ApprovedParameters(BaseModel):
    """Exactly what was approved.

    Snapshotted so that FR-022 can detect any post-approval change: if a parameter differs at
    execution time, the approval is void and re-approval is required.
    """

    model_config = StrictModel

    tenant_id: Annotated[str, Field(pattern=_GUID)]
    subscription_id: Annotated[str, Field(pattern=_GUID)]
    region: Annotated[str, Field(min_length=1)]
    fabric_capacity_sku: Annotated[str, Field(min_length=2)]
    environment: Annotated[str, Field(min_length=1)]
    monthly_total_aud: Annotated[float, Field(ge=0)]


class Approval(BaseModel):
    """A human authorisation bound to one plan identity (FR-020).

    ``tenant_id`` is a top-level field, denormalised from ``approved_parameters.tenant_id``, for
    the same reason ``SealedDeploymentPlan`` carries its own top-level ``tenant_id`` rather than
    relying on a nested one: the persistence layer's Cosmos partition key (FR-032) needs one
    unambiguous field to read, not a path into "what was approved" that happens to also identify
    who it belongs to.

    No separate ``plan_id`` GUID: the original design notes' illustrative table lists one alongside
    ``planHash``, but ``SealedDeploymentPlan`` (built later, after that table was written)
    deliberately has no such field — ``groundwork_orchestrator.state.repositories``'s own
    docstring records the decision that ``plan_hash`` is the sole plan identity, since it is
    already the content-derived identity FR-012/FR-020 bind approval to and a second, GUID-shaped
    id would just be a redundant alias with no field of its own to derive it from. ``plan_hash``
    below is that identity.
    """

    model_config = StrictModel

    approval_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    plan_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    approving_identity: ApprovingIdentity
    approved_at: datetime
    channel: ApprovalChannel
    artefact_uri: Annotated[str, Field(min_length=1)]
    approved_parameters: ApprovedParameters
    cost_estimate: CostEstimate
    threshold_applied: ThresholdPolicy
    second_approval: SecondApproval | None = None

    @model_validator(mode="after")
    def _tenant_id_matches_approved_parameters(self) -> Self:
        if self.tenant_id != self.approved_parameters.tenant_id:
            raise ValueError(
                f"tenant_id {self.tenant_id!r} does not match approved_parameters.tenant_id "
                f"{self.approved_parameters.tenant_id!r}; an approval cannot disagree with itself "
                f"about which tenant it belongs to"
            )
        return self

    @model_validator(mode="after")
    def _second_approver_present_and_distinct(self) -> Self:
        needed = self.threshold_applied.requires_second_approver(
            self.approved_parameters.monthly_total_aud,
            self.approved_parameters.environment,
        )
        if needed and self.second_approval is None:
            raise ValueError(
                f"monthly total {self.approved_parameters.monthly_total_aud} AUD meets the "
                f"{self.threshold_applied.monthly_amount_aud} AUD threshold; FR-020a requires a "
                f"second approver before execution"
            )
        if self.second_approval is not None:
            first = self.approving_identity.object_id
            second = self.second_approval.approving_identity.object_id
            if first == second:
                # SC-018: zero cases of a single identity satisfying both roles. Hard reject.
                raise ValueError(
                    "second approver must be a different identity from the first; one identity "
                    "satisfying both roles defeats separation of duties (FR-020a, SC-018)"
                )
        return self

    @model_validator(mode="after")
    def _licensing_disclosed_below_f64(self) -> Self:
        # FR-013d: a customer must not reach approval without being shown that Power BI viewers
        # need Pro or PPU below F64. Checked here because this is the last gate before execution.
        sku = self.approved_parameters.fabric_capacity_sku
        try:
            capacity_units = int(sku[1:])
        except (ValueError, IndexError) as exc:
            raise ValueError(f"unrecognised Fabric capacity SKU {sku!r}") from exc
        if capacity_units < 64 and not self.cost_estimate.powerbi_viewer_licensing_disclosed:
            raise ValueError(
                f"SKU {sku} is below F64, so Power BI viewers each require a Pro or PPU licence. "
                f"FR-013d requires this be disclosed before approval."
            )
        return self

    @property
    def is_self_approval(self) -> bool:
        """Whether one identity both requested and approved.

        Permitted below the threshold. The requesting identity is not held on this model, so the
        control plane supplies it; this property exists for the audit record's benefit.
        """
        return self.second_approval is None

    def authorises(self, plan_hash: str, parameters: ApprovedParameters) -> bool:
        """Whether this approval authorises the given plan and parameters (FR-020, FR-022).

        Any difference voids the approval. There is no partial or near-enough match.
        """
        return self.plan_hash == plan_hash and self.approved_parameters == parameters


class PendingApproval(BaseModel):
    """A first approval recorded while a second, distinct approver is still required (FR-020a).

    Not an :class:`Approval` in an unusual state — a genuinely different type. ``Approval``'s own
    ``_second_approver_present_and_distinct`` validator deliberately makes it impossible to
    construct an above-threshold ``Approval`` with no second approver, so that *any* code holding
    an ``Approval`` instance can trust it fully authorises a deployment with no further checking.
    That guarantee is worth keeping — which means the "first signature is in, second is still
    needed" state, which the API contract requires be representable (``POST .../approvals``
    returns `secondApprovalRequired: true` and stays open for a second, later call), needs its own
    type rather than weakening ``Approval``'s invariant to accommodate it.

    Persisted at the same ``approval_id`` in the same Cosmos container as the eventual
    ``Approval`` — see ``groundwork_orchestrator.state.repositories.pending_approval_repository``
    — and :meth:`complete` is the one path that turns this into a real, fully-validated
    ``Approval``, replacing the pending document at that id rather than creating a second one.
    """

    model_config = StrictModel

    approval_id: Annotated[str, Field(pattern=_GUID)]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    plan_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    approving_identity: ApprovingIdentity
    approved_at: datetime
    channel: ApprovalChannel
    artefact_uri: Annotated[str, Field(min_length=1)]
    approved_parameters: ApprovedParameters
    cost_estimate: CostEstimate
    threshold_applied: ThresholdPolicy

    @model_validator(mode="after")
    def _tenant_id_matches_approved_parameters(self) -> Self:
        if self.tenant_id != self.approved_parameters.tenant_id:
            raise ValueError(
                f"tenant_id {self.tenant_id!r} does not match approved_parameters.tenant_id "
                f"{self.approved_parameters.tenant_id!r}"
            )
        return self

    @model_validator(mode="after")
    def _actually_requires_a_second_approver(self) -> Self:
        # A PendingApproval that does NOT need a second approver is a caller error upstream — the
        # first approval should have been completed as an Approval directly. Modelling this as a
        # rejected construction rather than a silently-accepted one keeps the two types' scopes
        # from overlapping.
        if not self.threshold_applied.requires_second_approver(
            self.approved_parameters.monthly_total_aud, self.approved_parameters.environment
        ):
            raise ValueError(
                "this approval does not meet the second-approver threshold; it should have been "
                "recorded as a complete Approval, not a PendingApproval"
            )
        return self

    def complete(self, second_approval: SecondApproval) -> Approval:
        """Combine this pending record with a second approval into a fully-valid ``Approval``.

        Re-runs every ``Approval`` invariant (distinct-identity, licensing disclosure, threshold
        satisfaction) as the authoritative final check — this method does not duplicate any of
        that logic, it just supplies the field ``Approval`` was missing.
        """
        return Approval(
            approval_id=self.approval_id,
            tenant_id=self.tenant_id,
            plan_hash=self.plan_hash,
            approving_identity=self.approving_identity,
            approved_at=self.approved_at,
            channel=self.channel,
            artefact_uri=self.artefact_uri,
            approved_parameters=self.approved_parameters,
            cost_estimate=self.cost_estimate,
            threshold_applied=self.threshold_applied,
            second_approval=second_approval,
        )
