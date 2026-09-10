"""The ``networking`` stage (T079; FR-036): verifies the private-networking resources the
infrastructure stage (T077) already deployed, rather than deploying anything of its own.

**Rewritten 2026-08-24 (Clarifications, FR-038a) — disclosed narrowing, not a silent gap.**
This stage used to independently re-read the infrastructure stage's own
deployment stack (direct ARM ``GET``) and, via the ``azure-mgmt-network`` SDK, confirm each
private endpoint's live connection state was ``Approved`` — catching the case where a Deployment
Stack reports ``succeeded`` while Azure's own separate, asynchronous private-endpoint
connection-approval workflow has not actually finished. Under the new model System's credential
cannot make either of those calls any more (FR-006's bootstrap-only scope), so this stage can no
longer perform that specific check itself. It now verifies the same thing every other
verification-only stage in this group does — that the pipeline run ``infrastructure`` triggered
completed successfully — via ``stages/pipeline_execution.py``. **The narrower
private-endpoint-connection-approval check this stage used to make is a known, flagged gap**, not
silently dropped: either the pipeline's own YAML needs a task that performs it (using the
bootstrap identity's access, which the pipeline has and System no longer does), or Azure's
same-tenant auto-approval behaviour for the specific resources this blueprint provisions needs to
be confirmed sufficient on its own — neither has been decided here.

``blueprint.yaml``'s stage sequence keeps ``networking`` where it was (after ``infrastructure``,
before ``fabric``), even though it triggers nothing itself — ``fabric`` still depends on it in the
DAG, and this stage still gives that dependency a real, checked meaning (the shared run succeeded)
rather than removing the edge and losing that signal entirely.
"""

from __future__ import annotations

import asyncio

import httpx

from groundwork_contracts.audit import IdempotenceOutcome
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.pipeline_execution import (
    latest_run,
    poll_pipeline_run,
    stage_outcome_for,
)


class NetworkVerificationError(Exception):
    """A domain-level failure this stage recognised by name — no pipeline run has ever been
    triggered for this subscription, or the run did not reach a terminal state within budget."""


class NetworkingStage:
    """Implements ``Stage`` for the blueprint's ``networking`` stage. See module docstring for
    the 2026-08-24 rewrite and its disclosed narrowing."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 120,
    ) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        self._poll_interval_seconds = poll_interval_seconds
        self._max_poll_attempts = max_poll_attempts

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        organization_url = context.devops_organization_url
        if not organization_url:
            raise NetworkVerificationError(
                "no Azure DevOps organization URL for this deployment: neither the tenant "
                "record nor the worker-wide GROUNDWORK_DEVOPS_ORGANIZATION_URL setting "
                "provides one"
            )
        subscription_id = context.plan.subscription_id

        outcome = await latest_run(
            credential=context.credential,
            organization_url=organization_url,
            subscription_id=subscription_id,
            http_client=self._client,
        )
        if outcome is None:
            raise NetworkVerificationError(
                "no pipeline run found for this subscription; the infrastructure stage must "
                "have run and triggered one before this stage can verify it"
            )

        for _ in range(self._max_poll_attempts):
            if outcome.state == "completed":
                break
            await asyncio.sleep(self._poll_interval_seconds)
            outcome = await poll_pipeline_run(
                credential=context.credential,
                organization_url=organization_url,
                subscription_id=subscription_id,
                resume_token=outcome.resume_token,
                http_client=self._client,
            )
        else:
            raise NetworkVerificationError(
                f"pipeline run {outcome.run_id} did not reach a terminal state within the "
                f"poll budget ({self._max_poll_attempts} attempts at "
                f"{self._poll_interval_seconds}s)"
            )

        return stage_outcome_for(
            outcome, resources_affected=(), idempotence_outcome=IdempotenceOutcome.NO_OP
        )
