"""T030 — the live-environment integration test that closed docs/waf-assessment.md §3.4's own
long-standing gap: every other test in this suite runs `ReadinessEngine` against fakes. This one
runs it against real Azure.

**Read-only by construction, not by convention.** Every check in `CHECKS` (naming, network,
identity, tenant, quota, policy, devops, fabric) is a plan-time readiness assertion — it queries
ARM/Graph/DevOps and reports a verdict; none of them writes. No stage ever runs, no plan is ever
approved, nothing is ever deployed. This test proves the plan-time pipeline works against real
Azure APIs, not that the orchestrator does.

**Skipped by default, matching the `requires_azure` marker's own registered contract**
(pyproject.toml: "skipped unless real Azure credentials and a test subscription are present").
Set `GROUNDWORK_LIVE_TEST_SUBSCRIPTION_ID` to opt in — never a real customer subscription, a
disposable one you're willing to have read-only Azure/Graph/DevOps calls made against under
whatever identity `DefaultAzureCredential` resolves locally (matching the convention
`docs/release-checklist.md` §9 already states for `tests/idempotence`/`tests/resilience`).
Verified 2026-09-12 against the Groundwork platform's own `groundwork-dev` deployment.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from groundwork_contracts.readiness import ReadinessSummary, ValidationStatus
from groundwork_shared.config.readiness import load_landing_zone_contract
from groundwork_shared.config.settings import ReadinessSettings
from groundwork_shared.validation.engine import ReadinessEngine
from groundwork_shared.validation.registry import CHECKS, build_validation_context

_SUBSCRIPTION_ID = os.environ.get("GROUNDWORK_LIVE_TEST_SUBSCRIPTION_ID")

pytestmark = [
    pytest.mark.requires_azure,
    pytest.mark.skipif(
        not _SUBSCRIPTION_ID,
        reason=(
            "set GROUNDWORK_LIVE_TEST_SUBSCRIPTION_ID to a disposable test subscription "
            "to run this against real Azure — never a real customer subscription"
        ),
    ),
]


@pytest.fixture
def blueprint_root() -> Path:
    blueprints = Path(__file__).resolve().parents[2] / "infra" / "blueprints"
    return blueprints / "standard-production-fabric"


async def test_readiness_engine_runs_every_check_against_real_azure(blueprint_root: Path) -> None:
    """Every assertion in the real blueprint's contract must execute against real Azure and come
    back with a real verdict — not raise, and not silently report UNREACHABLE for every check,
    which would mean this test is accidentally still exercising a mock underneath."""
    from azure.identity.aio import DefaultAzureCredential

    contract = load_landing_zone_contract(blueprint_root / "assertions.yaml")
    engine = ReadinessEngine(contract, CHECKS)
    settings = ReadinessSettings(
        orchestrator_principal_id=os.environ.get("GROUNDWORK_ORCHESTRATOR_PRINCIPAL_ID", ""),
        devops_organization_url=os.environ.get("GROUNDWORK_DEVOPS_ORGANIZATION_URL"),
    )

    credential = DefaultAzureCredential()
    try:
        context = build_validation_context(
            tenant_id=os.environ.get("AZURE_TENANT_ID", ""),
            subscription_id=_SUBSCRIPTION_ID or "",
            region=os.environ.get("AZURE_LOCATION", "australiaeast"),
            credential=credential,
            readiness=settings,
        )
        summary: ReadinessSummary = await engine.evaluate(context, now=datetime.now(UTC))
    finally:
        await credential.close()

    # Every assertion in the contract produced a real result — the engine didn't skip or crash
    # partway through.
    assert len(summary.results) == len(contract.assertions)

    # Not every result is UNREACHABLE. UNREACHABLE is a legitimate individual verdict (a check
    # this subscription genuinely can't satisfy, e.g. no DevOps org connected yet), but *all* of
    # them being UNREACHABLE is the signature of a broken credential or a check that never
    # actually reached Azure — the exact failure mode this test exists to catch.
    statuses = {result.status for result in summary.results}
    assert statuses != {ValidationStatus.UNREACHABLE}, (
        "every check reported UNREACHABLE — this looks like the checks never reached real Azure "
        "at all (bad credential, wrong subscription, or a check silently still stubbed), not a "
        "genuinely unready environment"
    )
