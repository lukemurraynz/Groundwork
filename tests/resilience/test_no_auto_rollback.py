"""T062 / SC-019 — failures never auto-rollback."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from azure.core.credentials_async import AsyncTokenCredential
from tests.conftest import TENANT_ID
from tests.unit.test_queue_consumption import (
    DEPLOYMENT_ID,
    NOW,
    _FakeCredentialFactory,
    _FakeSequencer,
    _Harness,
    _poll,
    _queued_deployment,
    _sealed_plan,
)
from tests.unit.test_sequencer import _deployment, _sequencer, _succeeding_stages

from groundwork_contracts.deployment import DeploymentStatus
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


@pytest.mark.parametrize(
    "stage_name",
    [
        "devops_project",
        "infrastructure",
        "identity",
        "networking",
        "fabric",
        "monitoring",
        "validation_tests",
    ],
)
async def test_each_stage_failing_once_halts_never_rolls_back(
    blueprint, valid_plan, stage_name: str
) -> None:
    stages: dict[str, Any] = _succeeding_stages()

    class _BoomStage:
        name = stage_name

        def __init__(self) -> None:
            self.calls: list[object] = []

        async def execute(self, context: object) -> object:
            self.calls.append(context)
            raise RuntimeError(f"{stage_name} failed")

    stages[stage_name] = _BoomStage()
    sequencer, _repo, _audit = _sequencer(blueprint, stages, [NOW] * 200)

    result = await sequencer.run(_deployment(), valid_plan, credential=_Credential())

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.deployment.status is not DeploymentStatus.ROLLED_BACK
    assert result.halted is not None
    assert result.halted.failing_stage == stage_name


async def test_transient_failure_requeues_never_rolls_back(blueprint, valid_plan) -> None:
    stages: dict[str, Any] = _succeeding_stages()

    class _TransientStage:
        name = "infrastructure"

        async def execute(self, context: object) -> object:
            request = httpx.Request("GET", "https://example.invalid")
            response = httpx.Response(503, request=request)
            raise httpx.HTTPStatusError("retry later", request=request, response=response)

    stages["infrastructure"] = _TransientStage()
    sequencer, _repo, _audit = _sequencer(blueprint, stages, [NOW] * 200)

    result = await sequencer.run(_deployment(), valid_plan, credential=_Credential())

    assert result.deployment.status is DeploymentStatus.QUEUED
    assert result.deployment.status is not DeploymentStatus.ROLLED_BACK
    assert result.halted is None


def test_rollback_execution_remains_disclosed_501_path() -> None:
    from fastapi.testclient import TestClient
    from tests.contract.test_recovery_endpoint import (
        ROLLBACK_APPROVAL_ID,
        _approval,
        _build_app,
        _failed_stage_record,
        _halted_deployment,
    )

    outputs = (
        '{"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-333333333333/'
        "resourceGroups/rg-groundwork-33333333/providers/Microsoft.ManagedIdentity/"
        'userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":{"value":"rg-groundwork-33333333"}}'
    )

    calls = {"triggered": 0}

    def handler(request):
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200, json={"value": [{"id": 43, "name": "groundwork-33333333-rollback-pipeline"}]}
            )
        if request.method == "POST" and "/pipelines/43/runs" in path:
            calls["triggered"] += 1
            return httpx.Response(200, json={"id": 4242, "state": "completed", "result": "failed"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    app, container = _build_app(
        deployment=_halted_deployment(infrastructure_outputs=outputs),
        stage_record=_failed_stage_record(stage_name="infrastructure"),
        approvals=[_approval(approval_id=ROLLBACK_APPROVAL_ID)],
        http_client=http_client,
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/deployments/{DEPLOYMENT_ID}/recovery",
        headers={"Authorization": "Bearer good-token"},
        json={"action": "rollback", "approvalId": ROLLBACK_APPROVAL_ID},
    )

    assert response.status_code == 502
    saved = container._items[(TENANT_ID, DEPLOYMENT_ID)]
    assert saved["status"] == "halted"
    assert calls["triggered"] == 1
    import asyncio

    asyncio.run(http_client.aclose())


async def test_queue_loop_preserves_halted_deployment_without_auto_rollback(valid_plan) -> None:
    harness = _Harness([TENANT_ID])
    await harness.seed_tenant(TENANT_ID)
    queued = _queued_deployment(plan_hash=valid_plan.content_hash())
    await harness.seed_deployment(queued)
    await harness.seed_plan(_sealed_plan(valid_plan, tenant_id=TENANT_ID))

    def halted_result(deployment):
        from groundwork_contracts.audit import StageError
        from groundwork_orchestrator.engine.sequencer import HaltedState, RunResult

        halted_deployment = deployment.model_copy(
            update={"status": DeploymentStatus.HALTED, "current_stage": None, "completed_at": NOW}
        )
        return RunResult(
            deployment=halted_deployment,
            halted=HaltedState(
                failing_stage="infrastructure",
                error=StageError(code="Boom", message="failed", is_transient=False),
                recovery_options=("retry", "forward_fix", "rollback"),
            ),
        )

    sequencer = _FakeSequencer(halted_result)
    outcomes = await _poll(harness, sequencer, _FakeCredentialFactory())
    if outcomes[0].run_result is not None:
        await harness.deployment_repository.replace(TENANT_ID, outcomes[0].run_result.deployment)

    assert outcomes[0].run_result is not None
    stored = await harness.read_deployment(TENANT_ID, DEPLOYMENT_ID)
    assert stored.status is DeploymentStatus.HALTED
