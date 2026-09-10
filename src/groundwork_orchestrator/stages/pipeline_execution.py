"""Trigger-and-poll against the customer's own Azure DevOps pipeline (FR-038a, Clarifications
2026-08-24) — the shared mechanics every real provisioning stage from `infrastructure` onward now
uses instead of a direct ARM call. System's own credential never touches the customer's Azure
subscription past the one bootstrap write (FR-006a, `api/tenants.py`'s `bootstrap_identity` route);
this module only ever talks to the Azure DevOps REST API, using the same
``AZURE_DEVOPS_RESOURCE_ID``/no-PAT pattern ``devops_project.py`` already established.

**[VERIFIED]** against Microsoft Learn (retrieved 2026-08-24, see the research notes § V-009):
``POST {org}/{project}/_apis/pipelines/{pipelineId}/runs?api-version=7.1`` triggers a run;
``GET {org}/{project}/_apis/pipelines/{pipelineId}/runs/{runId}?api-version=7.1`` polls it. The
response carries two *independent* enums — ``state`` (``unknown|inProgress|canceling|completed``)
and ``result`` (``unknown|succeeded|failed|canceled``, only meaningful once ``state`` is
``completed``) — this module polls ``state`` to ``completed`` before ever inspecting ``result``,
the same two-phase distinction ``stages/infrastructure.py``'s own deployment-stack polling already
makes between an in-progress ``provisioningState`` and a terminal one.

**Disclosed scope boundary, not a silent gap.** ``main.bicep`` deploys
infrastructure, networking, identity, and monitoring resources in one apply (T078's own task
description) — so ``infrastructure`` is the one stage in this group that actually triggers a
pipeline run; ``networking``/``identity``/``monitoring`` are downstream verification stages that
confirm the *same* run converged, via :func:`poll_pipeline_run`, rather than triggering a
second redundant one. ``fabric`` is deliberately excluded from this file's reuse: its own 900-second
post-managed-private-endpoint-deletion retry interval (research notes § V-006) is a distinct
idempotence contract this shared trigger-and-poll flow does not yet model, and giving it a second,
independent pipeline run (rather than folding it into the shared one) needs an explicit decision,
not an assumption made here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import anyio
import httpx

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_contracts.deployment import Deployment
from groundwork_orchestrator.engine.sequencer import StageOutcome

PIPELINES_API_VERSION = "7.1"

# Duplicated, not imported, from devops_project.py/devops_pipelines.py — this module sits below
# both in the dependency graph (infrastructure.py, identity.py, and every other real-provisioning
# stage import it), and devops_project.py transitively imports devops_environments.py → fabric.py
# → infrastructure.py → this module, so importing from devops_project.py here creates a real
# circular import. Same discipline as infrastructure.py's own duplicated
# deployment_resource_group_name — must be kept byte-identical to what it duplicates.
AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"


def deployment_project_name(subscription_id: str) -> str:
    return f"groundwork-{subscription_id[:8]}"


def pipeline_name(project_name: str) -> str:
    return f"{project_name}-platform-release"


def rollback_pipeline_name(project_name: str) -> str:
    return f"{project_name}-rollback-pipeline"


_TERMINAL_RUN_STATE = "completed"
_SUCCEEDED_RESULT = "succeeded"


class PipelineExecutionError(Exception):
    """A domain-level failure this module recognised by name — the pipeline run itself reported
    ``failed``/``canceled``, or the pipeline could not be found."""


@dataclass(frozen=True, slots=True)
class PipelineRunOutcome:
    """What a trigger-or-poll call found — enough for a caller to build its own
    :class:`~groundwork_orchestrator.engine.sequencer.StageOutcome` without re-deriving the
    ``state``/``result`` split itself."""

    run_id: int
    state: str
    result: str | None
    resume_token: str
    """Opaque to callers — this module's own encoding of ``run_id``, so a later poll (a resumed
    deployment, or a downstream verification stage) can resolve back to the same run without
    re-triggering one."""


def resume_token_for(run_id: int) -> str:
    return f"ado-run:{run_id}"


def run_id_from_resume_token(resume_token: str) -> int:
    prefix = "ado-run:"
    if not resume_token.startswith(prefix):
        raise PipelineExecutionError(
            f"resume token {resume_token!r} is not a pipeline_execution run token "
            f"(expected {prefix!r} prefix)"
        )
    return int(resume_token.removeprefix(prefix))


async def _devops_headers(credential: Any) -> dict[str, str]:
    token = await credential.get_token(f"{AZURE_DEVOPS_RESOURCE_ID}/.default")
    return {"Authorization": f"Bearer {token.token}", "Content-Type": "application/json"}


async def _find_pipeline_id(
    http_client: httpx.AsyncClient,
    *,
    organization_url: str,
    project_name: str,
    name: str,
    headers: dict[str, str],
) -> int:
    url = f"{organization_url}/{project_name}/_apis/pipelines?api-version={PIPELINES_API_VERSION}"
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    for pipeline in response.json()["value"]:
        if pipeline["name"] == name:
            return int(pipeline["id"])
    raise PipelineExecutionError(
        f"pipeline {name!r} does not exist in project {project_name!r}; the devops_project stage "
        f"must have run and succeeded before this stage can trigger a run in it"
    )


async def trigger_pipeline_run(
    *,
    credential: Any,
    organization_url: str,
    subscription_id: str,
    http_client: httpx.AsyncClient,
    template_parameters: dict[str, Any] | None = None,
    pipeline_name_override: str | None = None,
) -> PipelineRunOutcome:
    """Start a fresh run of this subscription's platform-release pipeline. The caller is
    responsible for persisting the returned ``resume_token`` (via its own ``StageOutcome``) so a
    later poll — same deployment attempt resuming, or a downstream verification stage — resolves
    back to this exact run rather than starting a second one.

    ``template_parameters`` maps directly onto ``RunPipelineParameters.templateParameters`` —
    the exact mechanism ``infra/blueprints/standard-production-fabric/azure-pipelines.yml``'s own
    ``parameters:`` block (``applyChanges``, ``location``, ``resourceGroupName``,
    ``resourceToken``) expects a caller to supply.
    """
    project_name = deployment_project_name(subscription_id)
    name = pipeline_name_override or pipeline_name(project_name)
    headers = await _devops_headers(credential)

    pipeline_id = await _find_pipeline_id(
        http_client,
        organization_url=organization_url,
        project_name=project_name,
        name=name,
        headers=headers,
    )
    url = (
        f"{organization_url}/{project_name}/_apis/pipelines/{pipeline_id}/runs"
        f"?api-version={PIPELINES_API_VERSION}"
    )
    body: dict[str, Any] = {}
    if template_parameters:
        body["templateParameters"] = template_parameters
    response = await http_client.post(url, headers=headers, json=body)
    response.raise_for_status()
    response_body = response.json()
    run_id = int(response_body["id"])
    return PipelineRunOutcome(
        run_id=run_id,
        state=str(response_body.get("state", "unknown")),
        result=response_body.get("result"),
        resume_token=resume_token_for(run_id),
    )


async def poll_pipeline_run(
    *,
    credential: Any,
    organization_url: str,
    subscription_id: str,
    resume_token: str,
    http_client: httpx.AsyncClient,
    pipeline_name_override: str | None = None,
) -> PipelineRunOutcome:
    """Read one run's current state/result — never triggers a new run. Used both by the stage
    that owns triggering (to wait for its own run) and by downstream verification stages (to
    confirm the same run they never triggered has converged)."""
    project_name = deployment_project_name(subscription_id)
    run_id = run_id_from_resume_token(resume_token)
    headers = await _devops_headers(credential)

    # pipeline_name() returns a name, not the numeric id the URL needs — resolved via the same
    # lookup trigger_pipeline_run() performs, so a poll never depends on the caller having cached
    # the numeric id separately.
    pipeline_id = await _find_pipeline_id(
        http_client,
        organization_url=organization_url,
        project_name=project_name,
        name=pipeline_name_override or pipeline_name(project_name),
        headers=headers,
    )
    url = (
        f"{organization_url}/{project_name}/_apis/pipelines/{pipeline_id}/runs/{run_id}"
        f"?api-version={PIPELINES_API_VERSION}"
    )
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    body = response.json()
    return PipelineRunOutcome(
        run_id=run_id,
        state=str(body.get("state", "unknown")),
        result=body.get("result"),
        resume_token=resume_token,
    )


async def latest_run(
    *,
    credential: Any,
    organization_url: str,
    subscription_id: str,
    http_client: httpx.AsyncClient,
    pipeline_name_override: str | None = None,
) -> PipelineRunOutcome | None:
    """The most recently created run of this subscription's pipeline, or ``None`` if it has never
    run. Used by downstream verification stages (``networking``/``identity``/``monitoring``) that
    never trigger a run themselves — they confirm the run ``infrastructure`` already triggered
    converged, without needing an explicit resume-token handoff between sibling stages
    (``StageExecutionContext`` carries no such channel; re-deriving "the latest run" from Azure
    DevOps itself is simpler than inventing one).

    **[VERIFIED]** ``GET {org}/{project}/_apis/pipelines/{pipelineId}/runs?api-version=7.1`` lists
    runs — Microsoft's own docs do not document a sort order, so this treats the numerically
    highest ``id`` as the latest, matching Azure DevOps' own strictly-increasing run numbering.
    """
    project_name = deployment_project_name(subscription_id)
    headers = await _devops_headers(credential)
    pipeline_id = await _find_pipeline_id(
        http_client,
        organization_url=organization_url,
        project_name=project_name,
        name=pipeline_name_override or pipeline_name(project_name),
        headers=headers,
    )
    url = (
        f"{organization_url}/{project_name}/_apis/pipelines/{pipeline_id}/runs"
        f"?api-version={PIPELINES_API_VERSION}"
    )
    response = await http_client.get(url, headers=headers)
    response.raise_for_status()
    runs = response.json()["value"]
    if not runs:
        return None
    latest = max(runs, key=lambda r: int(r["id"]))
    run_id = int(latest["id"])
    return PipelineRunOutcome(
        run_id=run_id,
        state=str(latest.get("state", "unknown")),
        result=latest.get("result"),
        resume_token=resume_token_for(run_id),
    )


async def wait_for_pipeline_run(
    *,
    credential: Any,
    organization_url: str,
    subscription_id: str,
    outcome: PipelineRunOutcome,
    http_client: httpx.AsyncClient,
    max_poll_attempts: int,
    poll_interval_seconds: float,
    pipeline_name_override: str | None = None,
) -> PipelineRunOutcome:
    """Poll until a run reaches a terminal state, or fail with a named poll-budget error."""
    if outcome.state == _TERMINAL_RUN_STATE:
        return outcome
    for _ in range(max_poll_attempts):
        await anyio.sleep(poll_interval_seconds)
        outcome = await poll_pipeline_run(
            credential=credential,
            organization_url=organization_url,
            subscription_id=subscription_id,
            resume_token=outcome.resume_token,
            http_client=http_client,
            pipeline_name_override=pipeline_name_override,
        )
        if outcome.state == _TERMINAL_RUN_STATE:
            return outcome
    raise PipelineExecutionError(
        f"pipeline run {outcome.run_id} did not reach a terminal state within the poll budget "
        f"({max_poll_attempts} attempts at {poll_interval_seconds}s)"
    )


async def read_run_output_marker(
    *,
    credential: Any,
    organization_url: str,
    subscription_id: str,
    run_id: int,
    http_client: httpx.AsyncClient,
    marker: str,
    pipeline_name_override: str | None = None,
) -> str | None:
    """Read one prefixed line back from a completed run's logs."""
    project_name = deployment_project_name(subscription_id)
    headers = await _devops_headers(credential)
    resolved_pipeline_name = pipeline_name_override or pipeline_name(project_name)
    pipeline_id = await _find_pipeline_id(
        http_client,
        organization_url=organization_url,
        project_name=project_name,
        name=resolved_pipeline_name,
        headers=headers,
    )
    logs_url = (
        f"{organization_url}/{project_name}/_apis/pipelines/{pipeline_id}/runs/{run_id}/logs"
        f"?api-version={PIPELINES_API_VERSION}"
    )
    response = await http_client.get(logs_url, headers=headers)
    response.raise_for_status()
    logs_payload = response.json()
    logs = logs_payload.get("logs") or logs_payload.get("value") or []
    for log in logs:
        log_id = int(log["id"])
        detail_url = (
            f"{organization_url}/{project_name}/_apis/pipelines/{pipeline_id}/runs/{run_id}/logs/"
            f"{log_id}?$expand=signedContent&api-version={PIPELINES_API_VERSION}"
        )
        detail = await http_client.get(detail_url, headers=headers)
        detail.raise_for_status()
        detail_body = detail.json()
        signed_url = (detail_body.get("signedContent") or {}).get("url")
        if not isinstance(signed_url, str) or not signed_url:
            continue
        content = await http_client.get(signed_url)
        content.raise_for_status()
        for line in content.text.splitlines():
            if line.startswith(marker):
                return line.removeprefix(marker).strip()
    return None


def stage_outcome_for(
    outcome: PipelineRunOutcome,
    *,
    resources_affected: tuple[str, ...],
    idempotence_outcome: IdempotenceOutcome = IdempotenceOutcome.APPLIED,
    updated_deployment: Deployment | None = None,
) -> StageOutcome:
    """Translate one poll's ``state``/``result`` into the ``StageOutcome`` shape every Sequencer
    stage returns — the two-phase check T077 (and every stage in this group) must make: not
    ``completed`` yet is a caller concern (poll again later), never surfaced here as an outcome at
    all, since it isn't one yet.

    ``idempotence_outcome`` defaults to ``APPLIED`` (a stage that triggered the run — e.g.
    ``infrastructure``). Verification-only callers that never trigger anything themselves
    (``networking``/``identity``/``monitoring``) should pass ``NO_OP``, matching
    ``networking.py``'s own "always NO_OP on success" convention — there is nothing for *them* to
    have applied.
    """
    if outcome.state != _TERMINAL_RUN_STATE:
        raise PipelineExecutionError(
            f"pipeline run {outcome.run_id} is {outcome.state!r}, not yet 'completed' — caller "
            f"must keep polling, not treat this as an outcome"
        )
    if outcome.result == _SUCCEEDED_RESULT:
        return StageOutcome(
            status=StageStatus.SUCCEEDED,
            resume_token=outcome.resume_token,
            idempotence_outcome=idempotence_outcome,
            resources_affected=resources_affected,
            updated_deployment=updated_deployment,
        )
    from groundwork_contracts.audit import StageError

    return StageOutcome(
        status=StageStatus.FAILED,
        error=StageError(
            code="AdoPipelineRunFailed",
            message=f"pipeline run {outcome.run_id} completed with result "
            f"{outcome.result!r}, not 'succeeded'",
            is_transient=False,
        ),
    )
