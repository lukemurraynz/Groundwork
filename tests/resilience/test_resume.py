"""T060 / SC-010 / SC-016 — interrupted deployments resume from the last durable checkpoint."""

from __future__ import annotations

from typing import Any

import pytest
from azure.core.credentials_async import AsyncTokenCredential
from tests.conftest import TENANT_ID
from tests.unit.test_queue_consumption import (
    DEPLOYMENT_ID,
    NOW,
    _FakeCredentialFactory,
    _Harness,
    _poll,
    _sealed_plan,
)
from tests.unit.test_sequencer import (
    _deployment,
    _FakeContainer,
    _RaisingStage,
    _sequencer,
    _succeeding_stages,
)

from groundwork_contracts.audit import AuditRecord, DeploymentStageRecord
from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_orchestrator.engine.halt import requeue_after_recovery_choice
from groundwork_orchestrator.engine.queue_loop import DeploymentAttemptResult
from groundwork_orchestrator.engine.sequencer import Sequencer
from groundwork_orchestrator.state.audit_repository import AuditRepository
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

pytestmark = pytest.mark.resilience


@pytest.fixture
def blueprint():
    from pathlib import Path

    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


class _Credential(AsyncTokenCredential):
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        return type("_Token", (), {"token": "fake-token"})()

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _Credential:
        return self

    async def __aexit__(
        self,
        exc_type: object | None = None,
        exc: object | None = None,
        tb: object | None = None,
    ) -> None:
        return None


def _sequencer_for_harness(blueprint, deployment_repository, stages: dict[str, Any]):
    stage_record_repository = TenantScopedRepository(
        _FakeContainer(), model_cls=DeploymentStageRecord, id_field="record_id"
    )
    audit_repository = AuditRepository(
        TenantScopedRepository(_FakeContainer(), model_cls=AuditRecord, id_field="audit_id")
    )
    return Sequencer(
        blueprint,
        stages,
        __import__("tests.unit.test_sequencer", fromlist=["_FakeWhatIfCapture"])._FakeWhatIfCapture(
            ["https://example.invalid/whatif/ok.json"]
        ),
        deployment_repository=deployment_repository,
        stage_record_repository=stage_record_repository,
        audit_repository=audit_repository,
        actor_object_id="99999999-9999-9999-9999-999999999999",
        now_fn=lambda: NOW,
    )


async def test_resume_after_late_stage_exception_skips_completed_stages(
    blueprint, valid_plan
) -> None:
    first_run_stages: dict[str, Any] = _succeeding_stages()
    first_run_stages["monitoring"] = _RaisingStage(
        "monitoring", RuntimeError("simulated worker crash")
    )
    first_run_sequencer, _repo, _audit = _sequencer(blueprint, first_run_stages, [NOW] * 200)

    first_result = await first_run_sequencer.run(
        _deployment(), valid_plan, credential=_Credential()
    )

    assert first_result.deployment.status is DeploymentStatus.HALTED
    assert first_result.deployment.checkpoint is not None
    assert first_result.deployment.checkpoint.stage_name == "fabric"

    resumed = Deployment.model_validate(
        requeue_after_recovery_choice(first_result.deployment).model_dump()
    )
    resumed = resumed.model_copy(
        update={
            "status": DeploymentStatus.EXECUTING,
            "started_at": NOW,
            "lease": first_result.deployment.lease,
        }
    )
    second_run_stages: dict[str, Any] = _succeeding_stages()
    second_run_sequencer, _repo, _audit = _sequencer(blueprint, second_run_stages, [NOW] * 200)

    second_result = await second_run_sequencer.run(resumed, valid_plan, credential=_Credential())

    assert second_result.deployment.status is DeploymentStatus.SUCCEEDED
    for stage_name in ("devops_project", "infrastructure", "networking", "identity", "fabric"):
        assert len(second_run_stages[stage_name].calls) == 0
    assert len(second_run_stages["monitoring"].calls) == 1
    assert len(second_run_stages["validation_tests"].calls) == 1


async def test_state_store_outage_between_stages_recovers_then_resumes_from_checkpoint(
    blueprint, valid_plan
) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    from tests.unit.test_queue_consumption import _queued_deployment

    queued = _queued_deployment(plan_hash=valid_plan.content_hash())
    await harness.seed_deployment(queued)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=queued.tenant_id))

    stages = _succeeding_stages()
    sequencer = _sequencer_for_harness(blueprint, harness.deployment_repository, stages)
    original_upsert = harness.deployment_container.upsert_item
    failure_state = {"done": False}

    async def flaky_upsert(body: dict[str, Any], **kwargs: Any) -> Any:
        if (
            not failure_state["done"]
            and body.get("status") == "executing"
            and body.get("current_stage") == "infrastructure"
        ):
            failure_state["done"] = True
            raise RuntimeError("state store unavailable between stages")
        return await original_upsert(body, **kwargs)

    harness.deployment_container.upsert_item = flaky_upsert  # type: ignore[method-assign]
    first_poll = await _poll(harness, sequencer, _FakeCredentialFactory())
    assert [outcome.result for outcome in first_poll] == [DeploymentAttemptResult.ERROR]

    after_failure = await harness.read_deployment(queued.tenant_id, DEPLOYMENT_ID)
    assert after_failure.status is DeploymentStatus.EXECUTING
    assert after_failure.checkpoint is not None
    assert after_failure.checkpoint.stage_name == "devops_project"
    assert len(stages["devops_project"].calls) == 1
    assert len(stages["infrastructure"].calls) == 0

    second_poll = await _poll(harness, sequencer, _FakeCredentialFactory())
    assert [outcome.result for outcome in second_poll] == [DeploymentAttemptResult.RECOVERED]

    recovered = await harness.read_deployment(queued.tenant_id, DEPLOYMENT_ID)
    assert recovered.status is DeploymentStatus.QUEUED
    assert Deployment.model_validate(recovered.model_dump()) == recovered

    third_poll = await _poll(harness, sequencer, _FakeCredentialFactory())
    assert [outcome.result for outcome in third_poll] == [DeploymentAttemptResult.EXECUTED]

    final = await harness.read_deployment(queued.tenant_id, DEPLOYMENT_ID)
    assert final.status is DeploymentStatus.SUCCEEDED
    assert len(stages["devops_project"].calls) == 1
    assert len(stages["infrastructure"].calls) == 1
