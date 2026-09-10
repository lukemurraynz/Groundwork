"""FR-038a — the trigger-and-poll mechanics shared by every real provisioning stage from
``infrastructure`` onward. ``httpx.MockTransport`` stands in for Azure DevOps, the same convention
``test_devops_pipelines.py`` already uses.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import StageStatus
from groundwork_orchestrator.stages.pipeline_execution import (
    PipelineExecutionError,
    poll_pipeline_run,
    resume_token_for,
    rollback_pipeline_name,
    run_id_from_resume_token,
    stage_outcome_for,
    trigger_pipeline_run,
    wait_for_pipeline_run,
)

ORG_URL = "https://dev.azure.com/example-org"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
PROJECT_NAME = "groundwork-33333333"
PIPELINE_ID = 42
RUN_ID = 777


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


def test_resume_token_round_trips() -> None:
    token = resume_token_for(RUN_ID)
    assert run_id_from_resume_token(token) == RUN_ID


def test_rollback_pipeline_name_is_deterministic() -> None:
    assert rollback_pipeline_name(PROJECT_NAME) == f"{PROJECT_NAME}-rollback-pipeline"


def test_run_id_from_resume_token_rejects_a_foreign_token() -> None:
    with pytest.raises(PipelineExecutionError):
        run_id_from_resume_token("what-if:https://example.invalid/artefact.json")


async def test_trigger_pipeline_run_finds_and_starts() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
            )
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            return httpx.Response(200, json={"id": RUN_ID, "state": "inProgress", "result": None})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await trigger_pipeline_run(
            credential=_FakeCredential(),
            organization_url=ORG_URL,
            subscription_id=SUBSCRIPTION_ID,
            http_client=client,
        )

    assert outcome.run_id == RUN_ID
    assert outcome.state == "inProgress"
    assert outcome.resume_token == f"ado-run:{RUN_ID}"


async def test_trigger_pipeline_run_raises_when_pipeline_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"value": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(PipelineExecutionError, match="does not exist"):
            await trigger_pipeline_run(
                credential=_FakeCredential(),
                organization_url=ORG_URL,
                subscription_id=SUBSCRIPTION_ID,
                http_client=client,
            )


async def test_poll_pipeline_run_reads_state_and_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
            )
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            return httpx.Response(
                200, json={"id": RUN_ID, "state": "completed", "result": "succeeded"}
            )
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await poll_pipeline_run(
            credential=_FakeCredential(),
            organization_url=ORG_URL,
            subscription_id=SUBSCRIPTION_ID,
            resume_token=resume_token_for(RUN_ID),
            http_client=client,
        )

    assert outcome.state == "completed"
    assert outcome.result == "succeeded"


async def test_wait_for_pipeline_run_polls_to_terminal() -> None:
    calls = {"polls": 0}
    from groundwork_orchestrator.stages.pipeline_execution import PipelineRunOutcome

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={"value": [{"id": PIPELINE_ID, "name": rollback_pipeline_name(PROJECT_NAME)}]},
            )
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            calls["polls"] += 1
            state = "completed" if calls["polls"] == 2 else "inProgress"
            result = "succeeded" if state == "completed" else None
            return httpx.Response(200, json={"id": RUN_ID, "state": state, "result": result})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        outcome = await wait_for_pipeline_run(
            credential=_FakeCredential(),
            organization_url=ORG_URL,
            subscription_id=SUBSCRIPTION_ID,
            outcome=PipelineRunOutcome(
                run_id=RUN_ID,
                state="inProgress",
                result=None,
                resume_token=resume_token_for(RUN_ID),
            ),
            http_client=client,
            max_poll_attempts=3,
            poll_interval_seconds=0.0,
            pipeline_name_override=rollback_pipeline_name(PROJECT_NAME),
        )

    assert outcome.state == "completed"
    assert outcome.result == "succeeded"


def test_stage_outcome_for_succeeded_run() -> None:
    from groundwork_orchestrator.stages.pipeline_execution import PipelineRunOutcome

    outcome = PipelineRunOutcome(
        run_id=RUN_ID, state="completed", result="succeeded", resume_token=resume_token_for(RUN_ID)
    )

    stage_outcome = stage_outcome_for(outcome, resources_affected=("rg-groundwork-33333333",))

    assert stage_outcome.status is StageStatus.SUCCEEDED
    assert stage_outcome.resume_token == resume_token_for(RUN_ID)


def test_stage_outcome_for_failed_run() -> None:
    from groundwork_orchestrator.stages.pipeline_execution import PipelineRunOutcome

    outcome = PipelineRunOutcome(
        run_id=RUN_ID, state="completed", result="failed", resume_token=resume_token_for(RUN_ID)
    )

    stage_outcome = stage_outcome_for(outcome, resources_affected=())

    assert stage_outcome.status is StageStatus.FAILED
    assert stage_outcome.error is not None
    assert stage_outcome.error.is_transient is False


def test_stage_outcome_for_refuses_a_not_yet_completed_run() -> None:
    from groundwork_orchestrator.stages.pipeline_execution import PipelineRunOutcome

    outcome = PipelineRunOutcome(
        run_id=RUN_ID, state="inProgress", result=None, resume_token=resume_token_for(RUN_ID)
    )

    with pytest.raises(PipelineExecutionError, match="not yet 'completed'"):
        stage_outcome_for(outcome, resources_affected=())
