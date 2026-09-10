"""T021e — consent enforcement's execution-time half (FR-006 edge case).

``tests/security/test_consent_and_residency.py`` already pins the plan-time gate
(``ConsentState.permits_tenant_operations``, ``api/plans.py``). This file covers the other half of
the same edge case, which nothing exercised before: consent revoked *during* an in-flight
deployment must halt it before the next stage runs, not let execution continue on authority that no
longer exists.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from groundwork_contracts.audit import StageStatus
from groundwork_contracts.deployment import DeploymentStatus
from groundwork_contracts.tenant import AdoOrgAccessState, ConsentState, CustomerTenant
from groundwork_orchestrator.engine.sequencer import StageOutcome
from groundwork_shared.config.blueprints import load_blueprint
from tests.conftest import TENANT_ID
from tests.unit.test_sequencer import (
    _RESUME_TOKEN,
    STAGE_ORDER,
    _clock,
    _deployment,
    _FakeCredential,
    _FakeStage,
    _sequencer,
)

pytestmark = pytest.mark.asyncio


@pytest.fixture
def blueprint():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


def _tenant(consent_state: ConsentState) -> CustomerTenant:
    # ado_org_access_state defaults to GRANTED here — this file's own scenarios are about
    # consent_state specifically; FR-038b's independent gate has its own test file
    # (test_ado_org_access_gate.py) and must not cross-contaminate these.
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Consent Enforcement Test Tenant",
        consent_state=consent_state,
        consent_granted_at=(
            datetime(2026, 1, 1, tzinfo=UTC) if consent_state is ConsentState.GRANTED else None
        ),
        ado_org_access_state=AdoOrgAccessState.GRANTED,
        ado_org_access_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
    )


class _FakeTenantRepository:
    """Mutable so a test can flip ``consent_state`` between successive stage attempts within one
    ``Sequencer.run()`` call — exactly the "revoked mid-flight" scenario FR-006 names."""

    def __init__(self, tenant: CustomerTenant) -> None:
        self.tenant = tenant

    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None:
        assert tenant_id == item_id == TENANT_ID
        return self.tenant


def _succeeding_stages_that_revoke_after(
    tenant_repo: _FakeTenantRepository, revoke_after_stage: str
) -> dict[str, _FakeStage]:
    """Every stage succeeds; the fake tenant repository flips to REVOKED the instant
    ``revoke_after_stage`` finishes, simulating a customer revoking consent while the deployment is
    still running."""

    class _RevokingStage(_FakeStage):
        async def execute(self, context: Any) -> StageOutcome:
            outcome = await super().execute(context)
            if self.name == revoke_after_stage:
                tenant_repo.tenant = _tenant(ConsentState.REVOKED)
            return outcome

    return {
        name: _RevokingStage(
            name, [StageOutcome(status=StageStatus.SUCCEEDED, resume_token=_RESUME_TOKEN)]
        )
        for name in STAGE_ORDER
    }


async def test_revoked_consent_halts_before_the_next_stage(blueprint, valid_plan) -> None:
    tenant_repo = _FakeTenantRepository(_tenant(ConsentState.GRANTED))
    stages = _succeeding_stages_that_revoke_after(tenant_repo, revoke_after_stage="devops_project")
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert result.halted.failing_stage == "consent_check"
    assert result.deployment.status is DeploymentStatus.HALTED
    # devops_project ran (consent was still GRANTED); nothing after it did.
    assert stages["devops_project"].calls
    assert not stages["infrastructure"].calls
    assert not stages["identity"].calls


async def test_pending_consent_halts_before_any_stage_when_resuming(blueprint, valid_plan) -> None:
    """Not just REVOKED: PENDING (e.g. a tenant record edited back by mistake, or a race with
    onboarding) must refuse execution identically — ``permits_tenant_operations`` is only ever true
    for GRANTED, matching the plan-time gate's own contract."""
    tenant_repo = _FakeTenantRepository(_tenant(ConsentState.PENDING))
    stages = {
        name: _FakeStage(
            name, [StageOutcome(status=StageStatus.SUCCEEDED, resume_token=_RESUME_TOKEN)]
        )
        for name in STAGE_ORDER
    }
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert result.halted.failing_stage == "consent_check"
    assert not any(stage.calls for stage in stages.values())


async def test_no_tenant_repository_skips_the_check(blueprint, valid_plan) -> None:
    """``tenant_repository=None`` (unit tests elsewhere in this codebase that don't care about
    consent) must not break — the check is opt-in, not a hard dependency."""
    stages = {
        name: _FakeStage(
            name, [StageOutcome(status=StageStatus.SUCCEEDED, resume_token=_RESUME_TOKEN)]
        )
        for name in STAGE_ORDER
    }
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), tenant_repository=None)
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
