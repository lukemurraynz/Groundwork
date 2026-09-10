"""The DeploymentPlan — the deterministic-execution boundary object (ADR-0001).

This is the only structure a model may emit into orchestration. This class, not a separate JSON
Schema file, is the source of truth for that shape — call ``DeploymentPlan.model_json_schema()``
if you need it as JSON Schema.

Three design decisions here carry most of the weight:

**`extra="forbid"` everywhere.** An unexpected field means the model produced something the
orchestrator does not understand. That is a failure, not a warning to log and continue past.

**Fields the model must NOT supply are absent from the model entirely** — ``tenantId``,
``planHash``, ``approvalStatus``, ``validationSummary``, ``requestingIdentity``. Accepting
``tenantId`` from model output would let conversation content redirect the deployment target,
which is the precise attack the least-authority-per-tool rule (ADR-0012) exists to prevent.
Because these fields are not declared and extras are forbidden, an attempt to inject them is
rejected structurally rather
than by a check someone might forget to write. See :class:`SealedDeploymentPlan` for the
control-plane-owned envelope that carries them.

**Constrained value types.** ``PlanResource.properties`` accepts only scalars, and rejects any
string that looks like a template expression or command. A model cannot smuggle an ARM function
or a shell fragment through a parameter value (FR-026).
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.alias_generators import to_camel

from groundwork_contracts.blueprint import FabricCapacitySku

SCHEMA_VERSION: Final = "1.0.0"

# FR-012: a presented plan may be at most 24 hours old. Validation is re-run at approval
# regardless of age, because tenant state can drift inside the window.
PLAN_VALIDITY = timedelta(hours=24)

_GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

# Patterns that must never appear in a model-supplied parameter value. This is not a
# sanitiser — nothing is stripped or escaped. A match rejects the plan (FR-026).
_EXPRESSION_PATTERNS = (
    re.compile(r"\[.*\]", re.DOTALL),  # ARM template expression
    re.compile(r"\$\{.*\}", re.DOTALL),  # shell / Bicep-style interpolation
    re.compile(r"\$\("),  # command substitution
    re.compile(r"`"),  # backtick execution
    re.compile(
        r"(?:^|[\s;&|])(?:rm|curl|wget|iwr|Invoke-Expression|iex|bash|sh|pwsh|cmd)\b",
        re.I,
    ),
    re.compile(r"[;&|]{1,2}\s*\w"),  # command chaining
)

StrictModel = ConfigDict(
    extra="forbid",
    frozen=True,
    str_strip_whitespace=True,
    alias_generator=to_camel,
    populate_by_name=True,
    loc_by_alias=False,
)


class Environment(StrEnum):
    """Feeds the FR-020a approval threshold."""

    PRODUCTION = "production"
    NON_PRODUCTION = "non-production"


class ApprovedRegion(StrEnum):
    """Closed region set.

    Australian only, because FR-053b pins persisted conversation content to Australian regions
    and a plan targeting elsewhere cannot satisfy it. Cross-checked again at validation time
    against the specific tenant's ``approvedRegions``, since tenants may permit fewer.
    """

    AUSTRALIA_EAST = "australiaeast"
    AUSTRALIA_SOUTHEAST = "australiasoutheast"


class RiskSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


def _reject_expressions(value: str, *, field: str) -> str:
    for pattern in _EXPRESSION_PATTERNS:
        if pattern.search(value):
            raise ValueError(
                f"{field} contains what looks like a template expression, interpolation, or "
                f"command. Model output supplies constrained values only; executable content is "
                f"rejected, not sanitised (FR-026)."
            )
    return value


class PlanResource(BaseModel):
    """One resource the plan intends to create.

    Descriptive only. This drives plan presentation and ``what-if`` comparison; it is never
    executed and is not a template. The orchestrator executes pre-authored Bicep selected by
    blueprint, and uses this list to verify that what Azure reports matches what was approved.
    """

    model_config = StrictModel

    resource_type: Annotated[str, Field(pattern=r"^[A-Za-z0-9.]+/[A-Za-z0-9/]+$")]
    logical_name: Annotated[str, Field(min_length=1, max_length=260)]
    depends_on: tuple[str, ...] = ()
    properties: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _no_executable_content(self) -> Self:
        _reject_expressions(self.logical_name, field="logical_name")
        for key, value in self.properties.items():
            _reject_expressions(key, field=f"properties key {key!r}")
            if isinstance(value, str):
                _reject_expressions(value, field=f"properties[{key!r}]")
        return self


class RiskFinding(BaseModel):
    model_config = StrictModel

    description: Annotated[str, Field(min_length=1)]
    impact: Annotated[str, Field(min_length=1)]
    mitigation: str | None = None


class RiskAssessment(BaseModel):
    model_config = StrictModel

    severity: RiskSeverity
    findings: tuple[RiskFinding, ...] = ()

    @model_validator(mode="after")
    def _high_risk_needs_findings(self) -> Self:
        # A bare severity with no findings is an unexplained claim. If the agent judges the risk
        # medium or high, it must say what the risk actually is.
        if self.severity in (RiskSeverity.MEDIUM, RiskSeverity.HIGH) and not self.findings:
            raise ValueError(
                f"risk severity {self.severity.value!r} requires at least one finding explaining it"
            )
        return self


class StageDependency(BaseModel):
    model_config = StrictModel

    stage: Annotated[str, Field(min_length=1)]
    requires: tuple[str, ...]


class Clarification(BaseModel):
    """Audit of what the agent asked and was told.

    Recorded for traceability. Carries no authority whatsoever — an answer here cannot change the
    deployment target, approval requirements, or validation rules (FR-003).
    """

    model_config = StrictModel

    question: Annotated[str, Field(min_length=1)]
    answer: Annotated[str, Field(min_length=1)]
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] | None = None


class ValidityWindow(BaseModel):
    """Bounds how stale a presented plan may be (FR-012, FR-025)."""

    model_config = StrictModel

    not_before: datetime
    not_after: datetime

    @model_validator(mode="after")
    def _ordered_and_bounded(self) -> Self:
        if self.not_after <= self.not_before:
            raise ValueError("not_after must be later than not_before")
        if self.not_after - self.not_before > PLAN_VALIDITY:
            raise ValueError(f"validity window exceeds the {PLAN_VALIDITY} maximum set by FR-012")
        return self

    def is_valid_at(self, moment: datetime) -> bool:
        return self.not_before <= moment <= self.not_after


class CostEstimateRef(BaseModel):
    """Cost figures as emitted by the cost agent.

    Kept structurally identical to :class:`groundwork_contracts.approval.CostEstimate` but declared
    here so the plan schema is self-contained at the boundary. FR-018 makes the uncertainty band
    mandatory: a single precise figure is a defect, not a nicety.
    """

    model_config = StrictModel

    currency: Literal["AUD"] = "AUD"
    monthly_total: Annotated[float, Field(ge=0)]
    uncertainty_lower_pct: Annotated[float, Field(ge=0, le=100)]
    uncertainty_upper_pct: Annotated[float, Field(ge=0, le=100)]
    basis: Annotated[str, Field(min_length=1)]
    computed_at: datetime
    billed_to: Literal["customer"] = "customer"

    @model_validator(mode="after")
    def _band_must_be_meaningful(self) -> Self:
        # A zero-width band claims perfect precision about future cloud spend, which is never
        # true. FR-018 requires the estimate to state its uncertainty honestly.
        if self.uncertainty_lower_pct == 0 and self.uncertainty_upper_pct == 0:
            raise ValueError(
                "cost estimate declares a zero uncertainty band; FR-018 requires an honest band "
                "because an unqualified figure presented as precise is a defect"
            )
        return self

    def band_absolute(self) -> tuple[float, float]:
        """Return the (lower, upper) absolute bounds of the estimate."""
        lower = self.monthly_total * (1 - self.uncertainty_lower_pct / 100)
        upper = self.monthly_total * (1 + self.uncertainty_upper_pct / 100)
        return lower, upper

    def contains(self, amount: float) -> bool:
        """Whether ``amount`` falls inside the stated band.

        FR-019 uses this to decide re-approval: a re-checked figure inside the band was already
        accepted by the approver, so re-prompting them would fire on pricing noise.
        """
        lower, upper = self.band_absolute()
        return lower <= amount <= upper


class DeploymentPlan(BaseModel):
    """Schema-validated model output. The deterministic-execution boundary (ADR-0001).

    A model produces exactly this and nothing else. Note what is *not* here — see the module
    docstring. Those fields live on :class:`SealedDeploymentPlan`, which only the control plane
    constructs.
    """

    model_config = StrictModel

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    blueprint_id: Annotated[str, Field(pattern=r"^[a-z0-9-]+$")]
    blueprint_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    subscription_id: Annotated[str, Field(pattern=_GUID)]
    region: ApprovedRegion
    environment: Environment = Environment.PRODUCTION
    fabric_capacity_sku: FabricCapacitySku = FabricCapacitySku.F2
    resource_set: Annotated[tuple[PlanResource, ...], Field(min_length=1)]
    dependencies: tuple[StageDependency, ...] = ()
    estimated_duration_minutes: Annotated[int, Field(ge=1, le=240)]
    cost_estimate: CostEstimateRef
    risk_assessment: RiskAssessment
    clarifications_gathered: tuple[Clarification, ...] = ()

    @model_validator(mode="after")
    def _resource_names_unique(self) -> Self:
        names = [r.logical_name for r in self.resource_set]
        duplicates = {n for n in names if names.count(n) > 1}
        if duplicates:
            raise ValueError(
                f"duplicate logical resource names: {sorted(duplicates)}. Duplicates would make "
                f"the idempotence contract in FR-029 unverifiable."
            )
        return self

    @model_validator(mode="after")
    def _dependencies_reference_known_stages(self) -> Self:
        declared = {d.stage for d in self.dependencies}
        for dep in self.dependencies:
            unknown = set(dep.requires) - declared
            if unknown:
                raise ValueError(
                    f"stage {dep.stage!r} depends on undeclared stage(s) {sorted(unknown)}"
                )
        return self

    def content_hash(self) -> str:
        """Stable content-derived identity (FR-012).

        Excludes ``clarifications_gathered``: two customers may reach an identical plan through
        different conversations, and those plans are the same deployment. Including the transcript
        would make identity depend on how the customer phrased things, which would break the
        FR-020 approval binding for no benefit.
        """
        payload = self.model_dump(mode="json", exclude={"clarifications_gathered"})
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def requires_licensing_disclosure(self) -> bool:
        """Whether FR-013d's Power BI viewer-licensing disclosure applies.

        True below F64, where each viewer needs a Pro or PPU licence. Verified in
        the research notes § V-001.
        """
        return self.fabric_capacity_sku.capacity_units < FabricCapacitySku.F64.capacity_units


class SealedDeploymentPlan(BaseModel):
    """A validated plan plus the fields only the control plane may assert.

    The separation is the point. ``plan`` is what a model produced; everything alongside it is
    what the system determined. A model cannot reach these fields because it never emits this
    type — it emits :class:`DeploymentPlan`.
    """

    model_config = StrictModel

    plan: DeploymentPlan
    plan_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]
    tenant_id: Annotated[str, Field(pattern=_GUID)]
    requesting_identity_object_id: Annotated[str, Field(pattern=_GUID)]
    requesting_channel: str
    validity: ValidityWindow
    created_at: datetime

    @model_validator(mode="after")
    def _hash_matches_content(self) -> Self:
        from groundwork_contracts.errors import PlanIntegrityError

        actual = self.plan.content_hash()
        if actual != self.plan_hash:
            raise PlanIntegrityError(expected=self.plan_hash, actual=actual)
        return self

    @classmethod
    def seal(
        cls,
        plan: DeploymentPlan,
        *,
        tenant_id: str,
        requesting_identity_object_id: str,
        requesting_channel: str,
        now: datetime | None = None,
    ) -> SealedDeploymentPlan:
        """Bind a validated plan to a tenant and identity taken from the session.

        ``tenant_id`` must come from the authenticated session. There is deliberately no code path
        that derives it from ``plan`` — the plan has no such field (FR-007).
        """
        moment = now or datetime.now(UTC)
        return cls(
            plan=plan,
            plan_hash=plan.content_hash(),
            tenant_id=tenant_id,
            requesting_identity_object_id=requesting_identity_object_id,
            requesting_channel=requesting_channel,
            validity=ValidityWindow(not_before=moment, not_after=moment + PLAN_VALIDITY),
            created_at=moment,
        )
