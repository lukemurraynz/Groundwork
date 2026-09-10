"""Readiness assertion evaluation engine shared by control plane and orchestrator.

Copied out of ``groundwork_controlplane.validation`` so deterministic orchestrator code can reuse
the same readiness contract machinery without importing the control-plane package.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.readiness import (
    LandingZoneContract,
    ReadinessSummary,
    ValidationResult,
    ValidationStatus,
)
from groundwork_shared.telemetry.scrubbing import scrub_text


@dataclass(frozen=True, slots=True)
class ValidationContext:
    """What a check needs to evaluate one assertion against one real target environment."""

    tenant_id: str
    subscription_id: str
    region: str
    vnet_address_space: str
    deployment_identity_object_id: str
    credential: AsyncTokenCredential
    devops_organization_url: str | None = None
    required_resource_providers: tuple[str, ...] | None = None


CheckFunction = Callable[[ValidationContext], Awaitable[tuple[ValidationStatus, str]]]


class ReadinessEngine:
    """Evaluates a :class:`LandingZoneContract` against a real target environment."""

    def __init__(self, contract: LandingZoneContract, checks: dict[str, CheckFunction]) -> None:
        missing = {a.assertion_id for a in contract.assertions} - set(checks)
        if missing:
            raise ValueError(
                f"no check function registered for assertion(s) {sorted(missing)}; every "
                f"assertion in the contract must be executable, or the readiness report claims "
                f"to check something it cannot (FR-014a)"
            )
        self._contract = contract
        self._checks = checks

    async def evaluate(self, context: ValidationContext, *, now: datetime) -> ReadinessSummary:
        results: list[ValidationResult] = []
        for assertion in self._contract.assertions:
            check = self._checks[assertion.assertion_id]
            try:
                status, finding = await check(context)
            except Exception as exc:
                status = ValidationStatus.UNREACHABLE
                finding = scrub_text(f"check raised {type(exc).__name__}: {exc}")

            results.append(
                ValidationResult(
                    assertion_id=assertion.assertion_id,
                    contract_version=self._contract.contract_version,
                    design_area=assertion.design_area,
                    status=status,
                    severity=assertion.severity,
                    finding=finding,
                    remediation=(assertion.remediation if status.is_blocking_failure else None),
                    evaluated_at=now,
                )
            )

        return ReadinessSummary(
            contract_version=self._contract.contract_version,
            results=tuple(results),
            evaluated_at=now,
        )
