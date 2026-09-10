"""T035 — the readiness assertion evaluation engine (FR-014b, FR-015).

A fake credential and check functions stand in for real Azure SDK calls, same reasoning as
``tests/security/test_credential_scoping.py``: this proves the engine's own dispatch and
exception-to-UNREACHABLE mapping, not any particular Azure API.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from groundwork_contracts.readiness import (
    AssertionSeverity,
    DesignArea,
    LandingZoneContract,
    ReadinessAssertion,
    ValidationStatus,
)
from groundwork_controlplane.validation.engine import ReadinessEngine, ValidationContext

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class FakeCredential:
    def get_token(self, *scopes: str, **kwargs: object) -> object:
        return object()


def _context() -> ValidationContext:
    return ValidationContext(
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id="33333333-3333-3333-3333-333333333333",
        region="australiaeast",
        vnet_address_space="10.0.0.0/16",
        deployment_identity_object_id="44444444-4444-4444-4444-444444444444",
        credential=FakeCredential(),  # type: ignore[arg-type]
    )


def _assertion(
    assertion_id: str, design_area: DesignArea, **overrides: object
) -> ReadinessAssertion:
    kwargs: dict[str, object] = {
        "assertion_id": assertion_id,
        "design_area": design_area,
        "description": "A sufficiently long description of what is checked.",
        "remediation": "A sufficiently long remediation the customer can act on.",
        "severity": AssertionSeverity.BLOCKING,
    }
    kwargs.update(overrides)
    return ReadinessAssertion(**kwargs)  # type: ignore[arg-type]


def _single_assertion_contract(assertion: ReadinessAssertion) -> LandingZoneContract:
    """One real assertion plus a filler for every other design area, so the
    every-design-area-covered validator on LandingZoneContract is satisfied without every test
    needing to know about all eight areas."""
    fillers = [
        _assertion(f"filler.{area.value}", area, severity=AssertionSeverity.ADVISORY)
        for area in DesignArea
        if area != assertion.design_area
    ]
    return LandingZoneContract(contract_version="1.0.0", assertions=(assertion, *fillers))


async def _noop_check(_: ValidationContext) -> tuple[ValidationStatus, str]:
    return ValidationStatus.PASSED, "filler check always passes"


def _checks_for(contract: LandingZoneContract, target_id: str, target_check) -> dict:  # type: ignore[no-untyped-def]
    return {
        a.assertion_id: (target_check if a.assertion_id == target_id else _noop_check)
        for a in contract.assertions
    }


# --- construction ---------------------------------------------------------------


def test_engine_refuses_a_contract_with_no_check_for_an_assertion() -> None:
    assertion = _assertion("tenant.reachable", DesignArea.BILLING_AND_TENANT)
    contract = _single_assertion_contract(assertion)

    with pytest.raises(ValueError, match="no check function registered"):
        ReadinessEngine(contract, checks={})


# --- evaluation -------------------------------------------------------------------


async def test_passed_check_produces_no_remediation() -> None:
    assertion = _assertion("tenant.reachable", DesignArea.BILLING_AND_TENANT)
    contract = _single_assertion_contract(assertion)

    async def check(_: ValidationContext) -> tuple[ValidationStatus, str]:
        return ValidationStatus.PASSED, "subscription reachable"

    engine = ReadinessEngine(contract, _checks_for(contract, "tenant.reachable", check))
    summary = await engine.evaluate(_context(), now=NOW)

    result = next(r for r in summary.results if r.assertion_id == "tenant.reachable")
    assert result.status is ValidationStatus.PASSED
    assert result.remediation is None
    assert result.contract_version == "1.0.0"
    assert result.evaluated_at == NOW


async def test_failed_check_carries_its_assertions_remediation() -> None:
    assertion = _assertion(
        "network.vnet-overlap",
        DesignArea.NETWORK_TOPOLOGY,
        remediation="Choose a non-overlapping address range for the platform VNet.",
    )
    contract = _single_assertion_contract(assertion)

    async def check(_: ValidationContext) -> tuple[ValidationStatus, str]:
        return ValidationStatus.FAILED, "10.0.0.0/16 overlaps an existing VNet"

    engine = ReadinessEngine(contract, _checks_for(contract, "network.vnet-overlap", check))
    summary = await engine.evaluate(_context(), now=NOW)

    result = next(r for r in summary.results if r.assertion_id == "network.vnet-overlap")
    assert result.status is ValidationStatus.FAILED
    assert result.remediation == "Choose a non-overlapping address range for the platform VNet."
    assert result.blocks_deployment is True


async def test_a_raising_check_is_reported_unreachable_not_dropped() -> None:
    """FR-015 — a check that could not run must block exactly as a failed one does."""
    assertion = _assertion("devops.reachable", DesignArea.PLATFORM_AUTOMATION)
    contract = _single_assertion_contract(assertion)

    async def check(_: ValidationContext) -> tuple[ValidationStatus, str]:
        raise TimeoutError("Azure DevOps did not respond within 5s")

    engine = ReadinessEngine(contract, _checks_for(contract, "devops.reachable", check))
    summary = await engine.evaluate(_context(), now=NOW)

    result = next(r for r in summary.results if r.assertion_id == "devops.reachable")
    assert result.status is ValidationStatus.UNREACHABLE
    assert result.blocks_deployment is True
    assert result.remediation is not None
    assert "TimeoutError" in result.finding


async def test_unreachable_finding_is_scrubbed() -> None:
    """A provider error can contain a SAS URL or similar; FR-049 must hold even for engine-caught
    exceptions, not only for checks that remember to scrub their own error text."""
    assertion = _assertion("quota.registered", DesignArea.MANAGEMENT)
    contract = _single_assertion_contract(assertion)
    secret_guid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"  # noqa: S105 — a GUID, not a password

    async def check(_: ValidationContext) -> tuple[ValidationStatus, str]:
        raise RuntimeError(f"failed for tenant {secret_guid}")

    engine = ReadinessEngine(contract, _checks_for(contract, "quota.registered", check))
    summary = await engine.evaluate(_context(), now=NOW)

    result = next(r for r in summary.results if r.assertion_id == "quota.registered")
    assert secret_guid not in result.finding
    assert "REDACTED" in result.finding


async def test_every_assertion_in_the_contract_is_evaluated() -> None:
    assertion = _assertion("identity.no-standing-owner", DesignArea.IDENTITY_AND_ACCESS)
    contract = _single_assertion_contract(assertion)
    all_ids = [a.assertion_id for a in contract.assertions]
    engine = ReadinessEngine(contract, dict.fromkeys(all_ids, _noop_check))

    summary = await engine.evaluate(_context(), now=NOW)

    assert {r.assertion_id for r in summary.results} == set(all_ids)
    assert all(r.contract_version == "1.0.0" for r in summary.results)
