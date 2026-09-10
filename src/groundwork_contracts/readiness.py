"""Landing Zone Contract and validation results.

FR-014a evaluates readiness against a versioned contract, baselined on the eight Azure Landing Zone
design areas with Groundwork's own machine-checkable assertions beneath them. CAF publishes no
pass/fail conformance API (research notes § V-002), so claiming to "validate ALZ conformance" would
be overclaiming — the taxonomy is borrowed, the assertions are ours.

The single most important decision in this module: :class:`ValidationStatus` has **no**
``skipped`` member. A check that could not run is ``UNREACHABLE`` and blocks. The fail-fast
validation rule is that a validation which passes because it never reached the tenant is a
failure, not a pass — so the type system refuses to represent the alternative.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from groundwork_contracts.blueprint import DesignAreaName

StrictModel = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

DesignArea = DesignAreaName


class AssertionSeverity(StrEnum):
    BLOCKING = "blocking"
    ADVISORY = "advisory"


class ValidationStatus(StrEnum):
    """Outcome of one readiness check.

    There is no ``SKIPPED``. FR-015 requires a check that could not reach the target environment to
    be reported as failed, never as passed, and ``UNREACHABLE`` carries that meaning explicitly so
    it cannot be mistaken for a benign non-result.
    """

    PASSED = "passed"
    FAILED = "failed"
    UNREACHABLE = "unreachable"

    @property
    def is_blocking_failure(self) -> bool:
        """Both FAILED and UNREACHABLE block. Only PASSED does not."""
        return self is not ValidationStatus.PASSED


class ReadinessAssertion(BaseModel):
    """One machine-checkable readiness assertion.

    ``description`` and ``remediation`` are both required and length-floored. FR-016 requires
    findings that name the failing check and its remediation; a blank or vague remediation string
    would satisfy the schema while failing the requirement, so the floor is deliberate.
    """

    model_config = StrictModel

    assertion_id: Annotated[str, Field(pattern=r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")]
    design_area: DesignArea
    description: Annotated[str, Field(min_length=15)]
    remediation: Annotated[str, Field(min_length=15)]
    severity: AssertionSeverity


class LandingZoneContract(BaseModel):
    """A versioned set of readiness assertions.

    Versioned independently of CAF: the taxonomy is borrowed but the assertions are Groundwork's,
    so CAF revisions do not silently change what we claim to check.
    """

    model_config = StrictModel

    contract_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    assertions: Annotated[tuple[ReadinessAssertion, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _assertion_ids_unique(self) -> Self:
        ids = [a.assertion_id for a in self.assertions]
        duplicates = {i for i in ids if ids.count(i) > 1}
        if duplicates:
            raise ValueError(
                f"duplicate assertion ids: {sorted(duplicates)}. Results are keyed by id, so "
                f"duplicates would make a readiness report ambiguous."
            )
        return self

    @model_validator(mode="after")
    def _every_design_area_covered(self) -> Self:
        # A contract that silently omits a design area gives false assurance: the report would show
        # all-clear while never having examined, say, governance. Better to fail at load time.
        missing = set(DesignArea) - {a.design_area for a in self.assertions}
        if missing:
            raise ValueError(
                f"contract has no assertions for design area(s) "
                f"{sorted(m.value for m in missing)}. Every area must be covered, or the readiness "
                f"report implies assurance it does not have."
            )
        return self

    def blocking_assertions(self) -> tuple[ReadinessAssertion, ...]:
        return tuple(a for a in self.assertions if a.severity is AssertionSeverity.BLOCKING)


class ValidationResult(BaseModel):
    """Outcome of evaluating one assertion against the real target environment."""

    model_config = StrictModel

    assertion_id: Annotated[str, Field(min_length=1)]
    contract_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    design_area: DesignArea
    status: ValidationStatus
    severity: AssertionSeverity
    finding: Annotated[str, Field(min_length=1)]
    remediation: str | None = None
    evaluated_at: datetime

    @model_validator(mode="after")
    def _failures_carry_remediation(self) -> Self:
        # FR-016: a failure the customer cannot act on is not an actionable finding.
        if self.status.is_blocking_failure and not self.remediation:
            raise ValueError(
                f"result for {self.assertion_id!r} is {self.status.value} but carries no "
                f"remediation; FR-016 requires findings to name their remediation"
            )
        return self

    @property
    def blocks_deployment(self) -> bool:
        return self.status.is_blocking_failure and self.severity is AssertionSeverity.BLOCKING


class ReadinessSummary(BaseModel):
    """Aggregate readiness for a plan.

    ``deployable`` is computed, never supplied. FR-017 forbids presenting a plan as deployable
    while a blocking validation is unresolved, and a derived property cannot be set incorrectly by
    a caller in a hurry.
    """

    model_config = StrictModel

    contract_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    results: Annotated[tuple[ValidationResult, ...], Field(min_length=1)]
    evaluated_at: datetime

    @property
    def blocking_failures(self) -> tuple[ValidationResult, ...]:
        return tuple(r for r in self.results if r.blocks_deployment)

    @property
    def unreachable(self) -> tuple[ValidationResult, ...]:
        return tuple(r for r in self.results if r.status is ValidationStatus.UNREACHABLE)

    @property
    def deployable(self) -> bool:
        """True only when no blocking assertion failed and none was unreachable."""
        return not self.blocking_failures

    def counts(self) -> dict[str, int]:
        return {
            status.value: sum(1 for r in self.results if r.status is status)
            for status in ValidationStatus
        }


class DriftVerdict(StrEnum):
    STABLE = "stable"
    DRIFTED = "drifted"
    RECOVERED = "recovered"


class DriftSummary(BaseModel):
    """Latest readiness snapshot for one tenant/subscription pair."""

    model_config = StrictModel

    tenant_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")]
    subscription_id: Annotated[str, Field(pattern=r"^[0-9a-fA-F-]{36}$")]
    region: Annotated[str, Field(min_length=1)]
    verdict: DriftVerdict
    summary: ReadinessSummary

    @property
    def blocking_assertion_ids(self) -> tuple[str, ...]:
        return tuple(result.assertion_id for result in self.summary.blocking_failures)
