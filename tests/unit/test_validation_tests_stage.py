"""T083 — the ``validation_tests`` stage's read-only post-deploy health checks.

The Fabric capacity read is injected as a callable (``get_capacity``), the same seam
``fabric.py``'s ``ensure_capacity`` and ``networking.py``'s ``get_private_endpoint`` use —
``azure-mgmt-fabric``'s async pipeline is not httpx-compatible, so ``httpx.MockTransport`` cannot
stand in for it directly. The deployment-stack and Fabric-workspace reads go through the injected
``http_client`` via ``httpx.MockTransport``, the same convention as every other stage's test file.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.infrastructure import (
    API_VERSION as DEPLOYMENT_STACK_API_VERSION,
)
from groundwork_orchestrator.stages.infrastructure import ARM_ENDPOINT
from groundwork_orchestrator.stages.validation_tests import (
    ValidationTestsStage,
    ValidationTestsStageError,
)

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"  # matches conftest.SUBSCRIPTION_ID
STACK_NAME = "stack-groundwork-33333333"
CAPACITY_NAME = "fabricgw16fea16d024d"
WORKSPACE_NAME = "ws-groundwork-16fea16d024d"
STACK_URL = (
    f"{ARM_ENDPOINT}/subscriptions/{SUBSCRIPTION_ID}/providers/Microsoft.Resources"
    f"/deploymentStacks/{STACK_NAME}?api-version={DEPLOYMENT_STACK_API_VERSION}"
)


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


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


def _context(*, plan) -> StageExecutionContext:
    return StageExecutionContext(
        deployment=_deployment_stub(), plan=plan, credential=_FakeCredential(), attempt=1
    )


def _stack_response(state: str = "succeeded") -> dict[str, Any]:
    return {"name": STACK_NAME, "properties": {"provisioningState": state}}


def _capacity(state: str = "Active") -> SimpleNamespace:
    return SimpleNamespace(properties=SimpleNamespace(state=state))


def _workspaces_response(names: list[str]) -> dict[str, Any]:
    return {"value": [{"displayName": n, "id": f"ws-{n}"} for n in names]}


def _handler(*, stack_state: str = "succeeded", workspace_names: list[str] | None = None):
    if workspace_names is None:
        workspace_names = [WORKSPACE_NAME]

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "deploymentStacks" in url:
            return httpx.Response(200, json=_stack_response(stack_state))
        if url.endswith("/workspaces"):
            return httpx.Response(200, json=_workspaces_response(workspace_names))
        raise AssertionError(f"unexpected request: {request.method} {url}")

    return handler


async def test_all_checks_pass(valid_plan) -> None:
    async def fake_get_capacity(credential, subscription_id, resource_group, cap_name) -> Any:
        assert subscription_id == SUBSCRIPTION_ID
        assert cap_name == CAPACITY_NAME
        return _capacity()

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler()))
    stage = ValidationTestsStage(http_client=http_client, get_capacity=fake_get_capacity)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert isinstance(outcome, StageOutcome)
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.NO_OP
    assert outcome.resources_affected == (STACK_NAME, CAPACITY_NAME, WORKSPACE_NAME)


async def test_raises_when_deployment_stack_missing(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": {"code": "NotFound"}})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = ValidationTestsStage(http_client=http_client)
    context = _context(plan=valid_plan)

    with pytest.raises(ValidationTestsStageError, match="does not exist"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_when_deployment_stack_not_succeeded(valid_plan) -> None:
    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler(stack_state="failed")))
    stage = ValidationTestsStage(http_client=http_client)
    context = _context(plan=valid_plan)

    with pytest.raises(ValidationTestsStageError, match="'failed'"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_when_fabric_capacity_not_active(valid_plan) -> None:
    async def fake_get_capacity(credential, subscription_id, resource_group, cap_name) -> Any:
        return _capacity(state="Paused")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler()))
    stage = ValidationTestsStage(http_client=http_client, get_capacity=fake_get_capacity)
    context = _context(plan=valid_plan)

    with pytest.raises(ValidationTestsStageError, match="'Paused'"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_when_fabric_capacity_check_itself_fails(valid_plan) -> None:
    """A check that could not even execute (e.g. a network error) must still count as failed —
    FR-034 — never be swallowed and treated as passed."""

    async def fake_get_capacity(credential, subscription_id, resource_group, cap_name) -> Any:
        raise RuntimeError("simulated transient network failure")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(_handler()))
    stage = ValidationTestsStage(http_client=http_client, get_capacity=fake_get_capacity)
    context = _context(plan=valid_plan)

    with pytest.raises(RuntimeError, match="simulated transient network failure"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_when_fabric_workspace_not_found(valid_plan) -> None:
    async def fake_get_capacity(credential, subscription_id, resource_group, cap_name) -> Any:
        return _capacity()

    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(_handler(workspace_names=["some-other-workspace"]))
    )
    stage = ValidationTestsStage(http_client=http_client, get_capacity=fake_get_capacity)
    context = _context(plan=valid_plan)

    with pytest.raises(ValidationTestsStageError, match="was not found"):
        await stage.execute(context)

    await http_client.aclose()
