"""Bounded retry with transient-error classification (T072; FR-030).

Split into pure decision functions and the Cosmos-querying glue that feeds them, the same
discipline every readiness check in ``groundwork_shared.validation.checks.*`` already
follows: the query is Cosmos's job to get right, the retry/backoff rules are what deserve direct,
exhaustive testing.

**Why a failed-but-retryable stage returns the deployment to ``queued`` rather than retrying
in-process.** The blueprint's own ``minimum_retry_interval_seconds`` is not a small number for
every stage — the ``fabric`` stage declares 900 (research notes § V-006: a deleted Fabric managed
private endpoint cannot be recreated for at least 15 minutes). Blocking one worker's
``asyncio.Task`` inside a single ``Sequencer.run`` call for up to 900 seconds would tie up that
task and starve every other tenant's deployments from being considered during the wait — the
queue-consumption loop (``engine/queue_loop.py``) already polls every few seconds and iterates all
onboarded tenants each cycle, which is a far better fit for a long wait than an in-process sleep.
So a retryable failure here does the cheapest possible thing: record the failed attempt (FR-030 —
:class:`~groundwork_contracts.audit.DeploymentStageRecord` is append-only, so nothing is lost),
leave the deployment ``queued`` instead of ``halted``, and let a later poll cycle's own
``Sequencer.run`` call — which resumes from the last completed checkpoint exactly as any other
resume does — attempt the same stage again. ``retry_not_yet_due`` is the corresponding pre-check:
before even calling ``stage.execute()`` again, skip the attempt entirely (no wasted Azure call, no
wasted audit record) if the stage's own minimum interval has not yet elapsed since its last
recorded attempt.

**Classification is deliberately conservative.** Every stage-specific domain error (``*StageError``
— ``DevOpsProjectStageError``, ``InfrastructureStageError``, ``FabricStageError``, and so on) is
raised by a stage author specifically because retrying with the same inputs would fail identically
— none of them are classified transient here, and none need to be named individually: anything that
is not a recognised transient shape (a network-level failure, or an HTTP/Azure response explicitly
signalling the caller should retry) defaults to permanent, matching the conservative default
``Sequencer._run_one_stage`` already used before this module existed (``is_transient=False``).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import httpx
from azure.core.exceptions import HttpResponseError, ServiceRequestError, ServiceResponseError

from groundwork_contracts.audit import DeploymentStageRecord
from groundwork_orchestrator.state.repositories import StageRecordRepository

# Standard "the caller should retry" signals: request timeout, rate limiting, and the three
# server-side transient codes every major cloud API (including every Azure surface this codebase
# calls) documents as safe to retry. Not 501/505 (not implemented, version not supported — retrying
# an identical request cannot fix either).
_TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 429, 500, 502, 503, 504})


def is_transient_error(exc: BaseException) -> bool:
    """Whether ``exc`` represents a failure worth retrying with the same inputs.

    Checked in order of how specific the signal is:

    1. An HTTP response this codebase's own raw-``httpx`` stages raised via ``raise_for_status()``
       — transient only for the well-known retryable status codes.
    2. ``httpx.TransportError`` (connection refused, timeout, pool exhaustion, and every other
       subclass under it) — the request never got a response at all, which is the clearest
       "try again" signal there is.
    3. ``azure.core``'s own ``HttpResponseError`` (raised by every ``azure-mgmt-*``/``azure-core``
       SDK client this codebase uses — ``azure-mgmt-fabric``, ``azure-mgmt-network``) — same status
       code check as (1), via the exception's own ``status_code`` attribute.
    4. ``ServiceRequestError``/``ServiceResponseError`` — azure-core's own network-level failure
       types, the SDK-client equivalent of (2).

    Anything else — including every stage's own named domain exception — is not transient.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _TRANSIENT_HTTP_STATUS_CODES
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, HttpResponseError):
        return exc.status_code in _TRANSIENT_HTTP_STATUS_CODES
    return isinstance(exc, ServiceRequestError | ServiceResponseError)


def retry_not_yet_due(
    *,
    minimum_retry_interval_seconds: int,
    last_attempt_ended_at: datetime | None,
    now: datetime,
) -> datetime | None:
    """The earliest time a stage may be attempted again, or ``None`` if it is fine to attempt now.

    ``minimum_retry_interval_seconds`` of ``0`` (``BlueprintStage``'s own default — every stage
    except ``fabric`` this release) or no prior attempt at all both mean "no interval to wait out".
    """
    if minimum_retry_interval_seconds <= 0 or last_attempt_ended_at is None:
        return None
    earliest = last_attempt_ended_at + timedelta(seconds=minimum_retry_interval_seconds)
    return earliest if now < earliest else None


def should_retry_after_failure(
    *, is_transient: bool, retry_budget: int, attempts_so_far: int
) -> bool:
    """Whether a just-failed stage should be attempted again later, rather than halting now.

    ``attempts_so_far`` includes the attempt that just failed — a stage whose ``retry_budget`` is 3
    may be attempted 3 times total, not 3 times *after* the first failure.
    """
    return is_transient and attempts_so_far < retry_budget


@dataclass(frozen=True, slots=True)
class StageAttemptHistory:
    """What has already happened for one (deployment, stage) pair — enough to decide whether, and
    when, to attempt it again."""

    attempt_count: int
    last_ended_at: datetime | None


def _summarise(records: Sequence[DeploymentStageRecord]) -> StageAttemptHistory:
    ended_ats = [r.ended_at for r in records if r.ended_at is not None]
    return StageAttemptHistory(
        attempt_count=len(records),
        last_ended_at=max(ended_ats) if ended_ats else None,
    )


async def load_stage_attempt_history(
    stage_record_repository: StageRecordRepository,
    tenant_id: str,
    deployment_id: str,
    stage_name: str,
) -> StageAttemptHistory:
    """Query every prior :class:`DeploymentStageRecord` for this (deployment, stage) pair."""
    parameters = [
        {"name": "@deployment_id", "value": deployment_id},
        {"name": "@stage_name", "value": stage_name},
    ]
    query = "SELECT * FROM c WHERE c.deployment_id = @deployment_id AND c.stage_name = @stage_name"
    records = [
        record async for record in stage_record_repository.query(tenant_id, query, parameters)
    ]
    return _summarise(records)
