"""T079 — the ``networking`` stage.

**Rewritten 2026-08-24 (Clarifications, FR-038a)**: no longer reads the deployment stack or the
``azure-mgmt-network`` SDK directly (System's credential cannot touch the customer subscription
past bootstrap). Now verification-only against the same Azure DevOps pipeline run
``infrastructure`` triggered — identical shape to ``test_identity_stage.py``/
``test_pipeline_execution.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.networking import NetworkingStage, NetworkVerificationError

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"  # matches conftest.SUBSCRIPTION_ID
TENANT_ID = "11111111-1111-1111-1111-111111111111"
ORGANIZATION_URL = "https://dev.azure.com/groundwork-org"
PROJECT_NAME = "groundwork-33333333"
PIPELINE_ID = 42
RUN_ID = 999


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def _context(*, plan, organization_url: str | None = ORGANIZATION_URL) -> StageExecutionContext:
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
        tenant_id=TENANT_ID,
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


def _runs_list_response(*, state: str, result: str | None) -> httpx.Response:
    return httpx.Response(200, json={"value": [{"id": RUN_ID, "state": state, "result": result}]})


async def test_verifies_the_latest_run_succeeded(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            return _runs_list_response(state="completed", result="succeeded")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = NetworkingStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert isinstance(outcome, StageOutcome)
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.NO_OP


async def test_raises_when_no_run_has_ever_been_triggered(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            return httpx.Response(200, json={"value": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = NetworkingStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    with pytest.raises(NetworkVerificationError, match="no pipeline run"):
        await stage.execute(context)

    await http_client.aclose()


async def test_reports_failed_when_the_latest_run_failed(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipelines_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            return _runs_list_response(state="completed", result="failed")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = NetworkingStage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert outcome.status is StageStatus.FAILED


async def test_raises_without_a_devops_organization_url(valid_plan) -> None:
    http_client = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda r: (_ for _ in ()).throw(AssertionError("no HTTP call expected"))
        )
    )
    stage = NetworkingStage(http_client=http_client)
    context = _context(plan=valid_plan, organization_url=None)

    with pytest.raises(NetworkVerificationError, match="organization"):
        await stage.execute(context)

    await http_client.aclose()
