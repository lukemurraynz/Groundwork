"""T021g — FR-038b's Azure DevOps organisation access gate.

Independent of ``consent_state`` (Lighthouse delegation has no jurisdiction over Azure DevOps
organisation membership, a separate Entra-governed control plane) — this file exercises that
independence directly: a tenant with full Lighthouse consent but no ADO org access must still be
refused at ``devops_project``, and the reverse (ADO access granted, consent missing) must be refused
too, for the opposite reason.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from groundwork_contracts.audit import StageStatus
from groundwork_contracts.deployment import Checkpoint, DeploymentStatus
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

_GRANTED_AT = datetime(2026, 1, 1, tzinfo=UTC)


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


def _tenant(
    *, consent_state: ConsentState, ado_org_access_state: AdoOrgAccessState
) -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="ADO Org Access Gate Test Tenant",
        consent_state=consent_state,
        consent_granted_at=_GRANTED_AT if consent_state is ConsentState.GRANTED else None,
        ado_org_access_state=ado_org_access_state,
        ado_org_access_granted_at=(
            _GRANTED_AT if ado_org_access_state is AdoOrgAccessState.GRANTED else None
        ),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
    )


class _FakeTenantRepository:
    def __init__(self, tenant: CustomerTenant) -> None:
        self.tenant = tenant

    async def read(self, tenant_id: str, item_id: str) -> CustomerTenant | None:
        assert tenant_id == item_id == TENANT_ID
        return self.tenant


def _stages() -> dict[str, _FakeStage]:
    return {
        name: _FakeStage(
            name, [StageOutcome(status=StageStatus.SUCCEEDED, resume_token=_RESUME_TOKEN)]
        )
        for name in STAGE_ORDER
    }


async def test_missing_ado_org_access_halts_before_devops_project_even_with_consent(
    blueprint, valid_plan
) -> None:
    tenant = _tenant(
        consent_state=ConsentState.GRANTED, ado_org_access_state=AdoOrgAccessState.PENDING
    )
    tenant_repo = _FakeTenantRepository(tenant)
    stages = _stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert result.halted.failing_stage == "ado_org_access_check"
    assert result.deployment.status is DeploymentStatus.HALTED
    assert not any(stage.calls for stage in stages.values())


async def test_revoked_ado_org_access_halts_even_with_consent(blueprint, valid_plan) -> None:
    tenant = _tenant(
        consent_state=ConsentState.GRANTED, ado_org_access_state=AdoOrgAccessState.REVOKED
    )
    tenant_repo = _FakeTenantRepository(tenant)
    stages = _stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert result.halted.failing_stage == "ado_org_access_check"


async def test_missing_consent_halts_before_ado_org_access_is_even_checked(
    blueprint, valid_plan
) -> None:
    """Missing consent is caught by the consent_check gate, which runs first — the ado_org_access
    gate is never reached, so its own halt reason must not appear here."""
    tenant = _tenant(
        consent_state=ConsentState.PENDING, ado_org_access_state=AdoOrgAccessState.GRANTED
    )
    tenant_repo = _FakeTenantRepository(tenant)
    stages = _stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert result.halted.failing_stage == "consent_check"


async def test_both_granted_devops_project_runs(blueprint, valid_plan) -> None:
    tenant = _tenant(
        consent_state=ConsentState.GRANTED, ado_org_access_state=AdoOrgAccessState.GRANTED
    )
    tenant_repo = _FakeTenantRepository(tenant)
    stages = _stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert stages["devops_project"].calls


async def test_gate_only_applies_to_devops_project_not_other_stages(blueprint, valid_plan) -> None:
    """A tenant with ado_org_access still pending but who has already passed devops_project on a
    prior attempt (resuming past it) must not be blocked again — the gate only ever fires
    immediately before devops_project itself, never for stages after it."""
    tenant = _tenant(
        consent_state=ConsentState.GRANTED, ado_org_access_state=AdoOrgAccessState.PENDING
    )
    tenant_repo = _FakeTenantRepository(tenant)
    stages = _stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint, stages, _clock(), tenant_repository=tenant_repo
    )
    deployment = _deployment(
        checkpoint=Checkpoint(
            stage_name="devops_project",
            resume_token=_RESUME_TOKEN,
            recorded_at=_GRANTED_AT,
        )
    )

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.halted is None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert not stages["devops_project"].calls
