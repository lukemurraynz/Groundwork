"""T081 — the ``fabric`` stage's trigger-and-poll behavior (decision 2026-08-25: Fabric
provisions via the customer's own Azure DevOps pipeline with ``provisionFabric=true``, exactly
like ``infrastructure`` — never via direct ARM/Fabric calls from this service).

``httpx.MockTransport`` stands in for the Azure DevOps REST surface (pipeline lookup, run
trigger, run polling), the same convention as every other trigger-and-poll consumer of
``pipeline_execution.py``. The Fabric-side bash inside the pipeline is covered by the YAML
itself, not here.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import AuthorityChain, StageStatus
from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.fabric import (
    FabricStage,
    FabricStageError,
    capacity_name,
    key_vault_resource_id,
    workspace_name,
)

ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = "groundwork-33333333"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
PIPELINE_ID = 68
RUN_ID = 1163
ADMIN_UPN = "fabric-admin@customer-tenant.example"


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def _deployment_stub() -> Deployment:
    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    return Deployment(
        deployment_id="88888888-8888-8888-8888-888888888888",
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id=SUBSCRIPTION_ID,
        correlation_id="77777777-7777-7777-7777-777777777777",
        authority=AuthorityChain(
            plan_hash=f"sha256:{'a' * 64}",
            approval_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
        status=DeploymentStatus.EXECUTING,
        started_at=now,
        lease=SubscriptionLease(holder=SUBSCRIPTION_ID, expires_at=now + timedelta(hours=1)),
    )


def _context(*, plan, admin_upn: str | None = ADMIN_UPN, org_url: str | None = ORG_URL):
    return StageExecutionContext(
        deployment=_deployment_stub(),
        plan=plan,
        credential=_FakeCredential(),
        attempt=1,
        devops_organization_url=org_url,
        fabric_capacity_admin_upn=admin_upn,
    )


def _stage(http_client: httpx.AsyncClient) -> FabricStage:
    return FabricStage(capacity_admin_upn=None, http_client=http_client, poll_interval_seconds=0.0)


def test_naming_helpers_are_deterministic() -> None:
    assert capacity_name(SUBSCRIPTION_ID) == "fabricgw16fea16d024d"
    assert workspace_name(SUBSCRIPTION_ID) == "ws-groundwork-16fea16d024d"
    kv_id = key_vault_resource_id(SUBSCRIPTION_ID)
    assert kv_id.startswith(f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/")
    assert "/providers/Microsoft.KeyVault/vaults/kv-gw-" in kv_id


def _runs_api_handler(
    *,
    captured: dict[str, Any],
    polls_before_terminal: int,
) -> tuple[dict[str, int], Any]:
    calls = {"list_pipelines": 0, "trigger": 0, "poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            calls["list_pipelines"] += 1
            return httpx.Response(
                200,
                json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
            )
        if request.method == "POST" and path.endswith(f"/_apis/pipelines/{PIPELINE_ID}/runs"):
            calls["trigger"] += 1
            captured["json"] = request.read().decode()
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress"})
        if request.method == "GET" and path.endswith(
            f"/_apis/pipelines/{PIPELINE_ID}/runs/{RUN_ID}"
        ):
            calls["poll"] += 1
            if calls["poll"] <= polls_before_terminal:
                return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress"})
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "succeeded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    return calls, handler


async def test_triggers_run_with_fabric_parameters_and_polls_to_success(valid_plan) -> None:
    captured: dict[str, Any] = {}
    _, handler = _runs_api_handler(captured=captured, polls_before_terminal=1)
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert isinstance(outcome, StageOutcome)
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome.value == "applied"
    assert outcome.resume_token == f"ado-run:{RUN_ID}"
    assert capacity_name(SUBSCRIPTION_ID) in outcome.resources_affected
    # The triggered run must carry the fabric parameters the YAML block keys on.
    import json

    params = json.loads(captured["json"])["templateParameters"]
    assert params["applyChanges"] is True
    assert params["provisionFabric"] is True
    assert params["capacityAdminUpn"] == ADMIN_UPN
    assert params["fabricCapacitySku"] == valid_plan.fabric_capacity_sku.value
    assert params["location"] == valid_plan.region.value


async def test_failed_run_maps_to_failed_outcome(valid_plan) -> None:
    calls = {"poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
            )
        if request.method == "POST":
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress"})
        if request.method == "GET":
            calls["poll"] += 1
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "failed"}
            )
        raise AssertionError("unreachable")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client)

    outcome = await stage.execute(_context(plan=valid_plan))
    await http_client.aclose()

    assert outcome.status is StageStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.code == "AdoPipelineRunFailed"


async def test_missing_admin_upn_raises_named_error(valid_plan) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    stage = _stage(http_client)

    with pytest.raises(FabricStageError, match="capacity administrator"):
        await stage.execute(_context(plan=valid_plan, admin_upn=None))
    await http_client.aclose()


async def test_missing_organization_url_raises_named_error(valid_plan) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    stage = _stage(http_client)

    with pytest.raises(FabricStageError, match="organization URL"):
        await stage.execute(_context(plan=valid_plan, org_url=None))
    await http_client.aclose()
