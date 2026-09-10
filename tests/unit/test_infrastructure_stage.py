"""T077 — the ``infrastructure`` stage's trigger-and-poll against the customer's Azure DevOps
pipeline (rewritten 2026-08-24, FR-038a — no longer a direct ARM deployment-stack call).

``httpx.MockTransport`` stands in for the network, the same convention as
``test_pipeline_execution.py`` and ``test_devops_project_stage.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.infrastructure import (
    InfrastructureStage,
    InfrastructureStageError,
    deployment_resource_group_name,
    deployment_stack_name,
    resource_token,
)

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"  # matches conftest.SUBSCRIPTION_ID
STACK_NAME = "stack-groundwork-33333333"
ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = "groundwork-33333333"
PIPELINE_ID = 42
RUN_ID = 777


class _FakeCredential:
    async def __aenter__(self) -> _FakeCredential:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: object | None = None,
    ) -> None:
        del exc_type, exc_value, traceback

    async def close(self) -> None:
        return None

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def _context(*, plan, organization_url: str | None = ORG_URL) -> StageExecutionContext:
    return StageExecutionContext(
        deployment=_deployment_stub(),
        plan=plan,
        credential=_FakeCredential(),
        attempt=1,
        devops_organization_url=organization_url,
    )


def _deployment_stub():
    from datetime import UTC, datetime, timedelta

    from groundwork_contracts.audit import AuthorityChain
    from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease

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


def _pipelines_list_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
    )


def test_naming_helpers_are_deterministic() -> None:
    assert deployment_stack_name(SUBSCRIPTION_ID) == STACK_NAME
    assert deployment_resource_group_name(SUBSCRIPTION_ID) == "rg-groundwork-33333333"
    token = resource_token(SUBSCRIPTION_ID)
    assert len(token) == 12
    assert token == resource_token(SUBSCRIPTION_ID)


async def test_triggers_a_run_with_apply_changes_and_polls_to_success(valid_plan) -> None:
    triggered_params: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            body = json.loads(request.content)
            triggered_params.update(body.get("templateParameters", {}))
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        if request.method == "GET" and path.endswith(f"/runs/{RUN_ID}/logs"):
            return httpx.Response(200, json={"logs": [{"id": 1}]})
        if request.method == "GET" and path.endswith(f"/runs/{RUN_ID}/logs/1"):
            return httpx.Response(
                200,
                json={"signedContent": {"url": "https://logs.example/1"}},
            )
        if request.method == "GET" and str(request.url) == "https://logs.example/1":
            return httpx.Response(
                200,
                text=(
                    'GROUNDWORK_STACK_OUTPUTS={"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-groundwork-33333333/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":{"value":"rg-groundwork-33333333"}}\n'
                ),
            )
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "succeeded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = InfrastructureStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert isinstance(outcome, StageOutcome)
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.APPLIED
    assert STACK_NAME in outcome.resources_affected
    assert triggered_params["applyChanges"] is True
    assert triggered_params["resourceGroupName"] == "rg-groundwork-33333333"
    assert triggered_params["resourceToken"] == resource_token(SUBSCRIPTION_ID)
    assert outcome.updated_deployment is not None
    assert outcome.updated_deployment.infrastructure_outputs == (
        '{"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-groundwork-33333333/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":{"value":"rg-groundwork-33333333"}}'
    )


async def test_polls_through_in_progress_states_to_success(valid_plan) -> None:
    poll_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        if request.method == "GET" and path.endswith(f"/runs/{RUN_ID}/logs"):
            return httpx.Response(200, json={"logs": [{"id": 1}]})
        if request.method == "GET" and path.endswith(f"/runs/{RUN_ID}/logs/1"):
            return httpx.Response(200, json={"signedContent": {"url": "https://logs.example/2"}})
        if request.method == "GET" and str(request.url) == "https://logs.example/2":
            return httpx.Response(
                200,
                text=(
                    'GROUNDWORK_STACK_OUTPUTS={"managedIdentityResourceId":{"value":"/subscriptions/33333333-3333-3333-3333-333333333333/resourceGroups/rg-groundwork-33333333/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-abcdef123456"},"resourceGroupName":{"value":"rg-groundwork-33333333"}}\n'
                ),
            )
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            poll_count["n"] += 1
            if poll_count["n"] < 2:
                return httpx.Response(
                    200, json={"id": RUN_ID, "state": "inProgress", "result": None}
                )
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "succeeded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = InfrastructureStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert outcome.status is StageStatus.SUCCEEDED
    assert poll_count["n"] == 2


async def test_raises_when_run_completes_failed(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "failed"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = InfrastructureStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert outcome.status is StageStatus.FAILED
    assert outcome.error is not None
    assert outcome.error.is_transient is False


async def test_raises_when_poll_budget_is_exhausted(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = InfrastructureStage(
        http_client=http_client, poll_interval_seconds=0.0, max_poll_attempts=2
    )
    context = _context(plan=valid_plan)

    with pytest.raises(InfrastructureStageError, match="poll budget"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_without_a_devops_organization_url(valid_plan) -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(AssertionError("no HTTP call expected"))
        )
    )
    stage = InfrastructureStage(http_client=http_client)
    context = _context(plan=valid_plan, organization_url=None)

    with pytest.raises(InfrastructureStageError, match="organization"):
        await stage.execute(context)

    await http_client.aclose()
