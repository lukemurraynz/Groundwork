"""T072 — bounded retry with transient-error classification, tested in isolation from the
sequencer. See ``engine/retry.py``'s own module docstring for the requeue-not-in-process-sleep
design this exists to verify.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError

from groundwork_contracts.audit import DeploymentStageRecord, StageStatus
from groundwork_orchestrator.engine.retry import (
    is_transient_error,
    load_stage_attempt_history,
    retry_not_yet_due,
    should_retry_after_failure,
)
from groundwork_orchestrator.state.cosmos import TenantScopedRepository

NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"


# --- is_transient_error -------------------------------------------------------------------


def test_httpx_status_error_is_transient_for_retryable_codes() -> None:
    for status_code in (408, 429, 500, 502, 503, 504):
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(status_code, request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        assert is_transient_error(exc) is True, status_code


def test_httpx_status_error_is_permanent_for_client_errors() -> None:
    for status_code in (400, 401, 403, 404, 409, 422):
        request = httpx.Request("GET", "https://example.test")
        response = httpx.Response(status_code, request=request)
        exc = httpx.HTTPStatusError("boom", request=request, response=response)
        assert is_transient_error(exc) is False, status_code


def test_httpx_transport_error_is_transient() -> None:
    assert is_transient_error(httpx.ConnectError("connection refused")) is True
    assert is_transient_error(httpx.ConnectTimeout("timed out")) is True
    assert is_transient_error(httpx.ReadTimeout("timed out")) is True


def test_azure_http_response_error_follows_status_code() -> None:
    transient = HttpResponseError(message="throttled")
    transient.status_code = 429
    assert is_transient_error(transient) is True

    permanent = HttpResponseError(message="forbidden")
    permanent.status_code = 403
    assert is_transient_error(permanent) is False


def test_azure_service_level_errors_are_transient() -> None:
    assert is_transient_error(ServiceRequestError("could not connect")) is True
    assert is_transient_error(ServiceResponseError("no response")) is True


def test_domain_specific_stage_errors_are_not_transient() -> None:
    """Every stage's own named exception (DevOpsProjectStageError, FabricStageError, ...) is not
    a recognised transient shape and must default to permanent — a stage author raised it
    specifically because retrying with the same inputs would fail identically."""

    class FabricStageError(Exception):
        pass

    class InfrastructureStageError(Exception):
        pass

    assert is_transient_error(FabricStageError("capacity not visible yet")) is False
    assert is_transient_error(InfrastructureStageError("stack failed")) is False
    assert is_transient_error(ValueError("something else entirely")) is False


# --- retry_not_yet_due ---------------------------------------------------------------------


def test_no_interval_means_never_not_due() -> None:
    assert (
        retry_not_yet_due(minimum_retry_interval_seconds=0, last_attempt_ended_at=NOW, now=NOW)
        is None
    )


def test_no_prior_attempt_means_never_not_due() -> None:
    assert (
        retry_not_yet_due(minimum_retry_interval_seconds=900, last_attempt_ended_at=None, now=NOW)
        is None
    )


def test_interval_not_yet_elapsed_returns_the_earliest_retry_time() -> None:
    earliest = retry_not_yet_due(
        minimum_retry_interval_seconds=900,
        last_attempt_ended_at=NOW,
        now=NOW + timedelta(seconds=1),
    )
    assert earliest == NOW + timedelta(seconds=900)


def test_interval_elapsed_returns_none() -> None:
    assert (
        retry_not_yet_due(
            minimum_retry_interval_seconds=900,
            last_attempt_ended_at=NOW,
            now=NOW + timedelta(seconds=901),
        )
        is None
    )


def test_interval_elapsed_exactly_at_the_boundary_returns_none() -> None:
    assert (
        retry_not_yet_due(
            minimum_retry_interval_seconds=900,
            last_attempt_ended_at=NOW,
            now=NOW + timedelta(seconds=900),
        )
        is None
    )


# --- should_retry_after_failure ------------------------------------------------------------


def test_transient_failure_within_budget_should_retry() -> None:
    assert should_retry_after_failure(is_transient=True, retry_budget=3, attempts_so_far=1) is True
    assert should_retry_after_failure(is_transient=True, retry_budget=3, attempts_so_far=2) is True


def test_transient_failure_at_budget_should_not_retry() -> None:
    """attempts_so_far includes the attempt that just failed — a budget of 3 permits 3 attempts
    total, not 3 retries after the first failure."""
    assert should_retry_after_failure(is_transient=True, retry_budget=3, attempts_so_far=3) is False


def test_permanent_failure_never_retries_regardless_of_budget() -> None:
    assert (
        should_retry_after_failure(is_transient=False, retry_budget=3, attempts_so_far=1) is False
    )


# --- load_stage_attempt_history ------------------------------------------------------------


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def query_items(
        self, query: str = "", *, parameters: list[dict[str, Any]] | None = None, **_: Any
    ):
        for doc in self.documents.values():
            if parameters and not all(
                doc.get(p["name"].lstrip("@")) == p["value"] for p in parameters
            ):
                continue
            yield doc


_record_counter = {"n": 0}


def _record(
    *, stage_name: str, attempt: int, deployment_id: str = DEPLOYMENT_ID, ended_at: datetime | None
) -> DeploymentStageRecord:
    _record_counter["n"] += 1
    return DeploymentStageRecord(
        record_id=f"cccccccc-cccc-cccc-cccc-{_record_counter['n']:012d}",
        deployment_id=deployment_id,
        tenant_id=TENANT_ID,
        stage_name=stage_name,
        attempt=attempt,
        status=StageStatus.SUCCEEDED,
        started_at=NOW,
        ended_at=ended_at,
    )


async def test_history_counts_only_matching_deployment_and_stage() -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    await repo.create(TENANT_ID, _record(stage_name="fabric", attempt=1, ended_at=NOW))
    await repo.create(
        TENANT_ID,
        _record(stage_name="fabric", attempt=2, ended_at=NOW + timedelta(seconds=10)),
    )
    # A different stage's record for the same deployment — must not be counted.
    await repo.create(TENANT_ID, _record(stage_name="devops_project", attempt=1, ended_at=NOW))
    # The same stage name, but a different deployment — must not be counted either.
    await repo.create(
        TENANT_ID,
        _record(
            stage_name="fabric",
            attempt=1,
            deployment_id="99999999-9999-9999-9999-999999999999",
            ended_at=NOW,
        ),
    )

    history = await load_stage_attempt_history(repo, TENANT_ID, DEPLOYMENT_ID, "fabric")

    assert history.attempt_count == 2
    assert history.last_ended_at == NOW + timedelta(seconds=10)


async def test_history_is_empty_for_a_never_attempted_stage() -> None:
    container = _FakeContainer()
    repo: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        container, model_cls=DeploymentStageRecord, id_field="record_id"
    )

    history = await load_stage_attempt_history(repo, TENANT_ID, DEPLOYMENT_ID, "fabric")

    assert history.attempt_count == 0
    assert history.last_ended_at is None
