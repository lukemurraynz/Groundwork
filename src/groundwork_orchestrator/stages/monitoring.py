"""The ``monitoring`` stage (T082; FR-040).

**Rewritten 2026-08-24 (Clarifications, FR-038a) — disclosed narrowing, not a silent gap.**
This stage is verification-only, confirming the same pipeline run
``infrastructure`` triggered converged — same shape as ``networking.py``/``identity.py``.

**Resolved 2026-08-26 against live Azure: the deferred diagnostic-settings write is impossible,
not pending.** After ``fabric.py``'s pipeline-side rewrite (T081, live 2026-08-25) gave the write a
home, a direct ARM check on the real capacity proved ``Microsoft.Fabric/capacities`` does not
support diagnostic settings at all — ARM rejects the resource type outright
(``ResourceTypeNotSupported: The resource type 'microsoft.fabric/capacities' does not support
diagnostic settings``). FR-040's diagnostics-on-capacity requirement cannot be satisfied via the
Azure Monitor diagnostic-settings API for this resource type; Fabric surfaces capacity telemetry
through its own monitoring instead. Reviving it is a spec-level design decision, not code here.

Original investigation preserved below for the reasoning that is still current (what
``main.bicep`` covers, why alert rules and tags are out of scope) — only the *mechanism* for the
one real write changed, not the scope investigation itself.

**What ``main.bicep`` (T077/T078) already covers.** It composes six AVM modules — resource group,
virtual network, Log Analytics workspace, Application Insights, a user-assigned managed identity,
and Key Vault — and wires ``diagnosticSettings`` for exactly one of them: the Key Vault (see its
``diagnosticSettings: [{ workspaceResourceId: logAnalytics.outputs.resourceId }]`` block). The other
five get no diagnostic settings of their own. Nothing in this session's investigation (no FR, no
task description, no prior stage's docstring) flags that omission as a defect — Log Analytics and
Application Insights are themselves the platform's two telemetry *sinks*, not resources that
meaningfully need their own diagnostic export, and the virtual network and managed identity have
been deliberately left as ``main.bicep``'s own scoping decision. This stage does not retrofit them;
doing so on no evidence of a real gap would be exactly the busywork ``networking.py``'s own
investigation avoided.

**What is genuinely missing: the Fabric capacity.** ``fabric.py`` (T081) creates
``Microsoft.Fabric/capacities/{capacity_name}`` and never once mentions monitoring, diagnostics, or
Log Analytics anywhere in its own docstring or code — confirmed by reading it in full. FR-040
("System MUST apply the required resource tagging and diagnostic settings to every resource it
creates") applies to the Fabric capacity exactly as much as to any AVM-created resource, and nothing
upstream of this stage has satisfied it. This is this stage's one real write.

**Verified 2026-08-01 — the diagnostic settings API surface.** Microsoft Learn's own
moniker-versioned pages were not consulted for this (``.apm/known-pitfalls.md``'s own recorded
reason: unreliable regardless of the ``?view=`` query); instead, verified against the versioned
TypeSpec source in ``Azure/azure-rest-api-specs`` (``specification/monitor/resource-manager/
Microsoft.Insights/Insights/DiagnosticsSettings/{DiagnosticSettingsResource,models}.tsp``),
cross-checked against ``az provider show --namespace Microsoft.Insights`` (which lists
``2021-05-01-preview`` as the newest, and only, version registered for the ``diagnosticSettings``
resource type — this API has never left preview, which the TypeSpec source's single
``examples/2021-05-01-preview/`` directory confirms is not a version this session
under-verified but the API's actual, permanent state):

- ``Microsoft.Insights/diagnosticSettings`` is an *extension resource*
  (``Extension.ScopeParameter``, ``scope: resourceUri`` in the TypeSpec source) — it is addressed as
  ``{targetResourceId}/providers/Microsoft.Insights/diagnosticSettings/{name}``, not as its own
  top-level resource. ``createOrUpdate`` is a synchronous create-or-replace
  (``Extension.CreateOrReplaceSync``) — no polling required, matching ``identity.py``'s federated
  credential write for the same reason (both are synchronous ARM ``PUT``s, unlike the Deployment
  Stacks and Azure DevOps operation APIs the sibling stages poll).
- ``DiagnosticsLogSettings`` (the ``properties.logs[]`` array) has a ``categoryGroup`` field;
  ``DiagnosticsMetricSettings`` (``properties.metrics[]``) does not — it only has ``category``, and
  the well-known value ``"AllMetrics"`` is itself the metric-category name every resource type
  exposes, not a category group. This stage submits ``logs: [{categoryGroup: "allLogs", enabled:
  true}]`` and ``metrics: [{category: "AllMetrics", enabled: true}]`` — the same generic,
  enumerate-nothing-per-resource-type shape AVM's own diagnostic-settings default already applies to
  the Key Vault in ``main.bicep`` (that block passes no explicit ``logCategoriesAndGroups``, relying
  on the AVM interface's own ``allLogs``/``AllMetrics`` default), so this stage's Fabric capacity
  write is consistent with the one diagnostic setting already deployed elsewhere in this blueprint,
  not a second, differently-shaped convention.

**Resolved 2026-08-26 — moot.** The question below assumed this stage still writes diagnostic
settings; since the 2026-08-24 rewrite it never touches them (see header), and the live ARM check
recorded there proved the write would be rejected for this resource type regardless.

**Why alert rules are not built here, despite ``blueprint.yaml``'s own prose.** Both the
``monitoring`` stage's ``requiredPermissions`` justification ("Configuring diagnostic settings and
alert rules on deployed resources") and its ``idempotenceContract`` ("Diagnostic settings and alert
rules are keyed by name...") name alert rules as part of this stage's job. Checked against every
other artefact that could ground that scope in something concrete — FR-040 (the only FR this task or
the original task plan's own T082 description cites: "applying required tags and diagnostic settings
(FR-040)", alert rules absent from that line too), the research notes, the original design
notes — and found nothing: no alert condition, threshold, metric, or notification target is
defined anywhere for a
customer-tenant resource. The only alerting functional requirement in the entire spec, FR-054
("System MUST alert operators on deployment failure rate, stage duration breach, queue depth...") is
explicitly Groundwork's own *operator*-facing alerting on its own service health — a different,
not-yet-built system (the original design notes themselves still describe it as unbuilt), not a
rule this stage would create inside a customer's tenant. Building a customer-tenant alert rule
here would mean inventing an arbitrary metric, threshold, and Action Group/notification target
with no source of
truth anywhere in this codebase — precisely the "no inventing scope the blueprint didn't declare"
prohibition, not a license to satisfy stale prose. This finding is reported, not silently dropped: a
future blueprint revision that wants real customer-tenant alerting needs to say what should alert,
at what threshold, and to whom, before a stage can build it.

**Why tags are not written here either, despite the original task plan's T082 description
mentioning them.**
Verified live 2026-08-01 (``az role definition list --name "Monitoring Contributor"``): the role's
full action list is ``*/read`` plus write actions scoped to ``Microsoft.Insights/*``,
``Microsoft.AlertsManagement/*``, ``Microsoft.Monitor/*``, a narrow slice of
``Microsoft.OperationalInsights/workspaces/*``, ``Microsoft.Support/*``, and
``Microsoft.Resources/deployments/*`` — no ``Microsoft.Fabric/capacities/write`` and no generic
``Microsoft.Resources/tags/write`` anywhere in it. A stage granted only ``Monitoring Contributor``
cannot write tags onto the Fabric capacity (or any other resource) even if it wanted to; the
permission ``blueprint.yaml`` itself grants this stage proves tags are not this stage's real scope,
regardless of how the task description summarised it. Separately confirmed: the Fabric capacity does
have a real, un-covered FR-040 tagging gap — ``fabric.py``'s ``_default_ensure_capacity`` never
passes ``tags=`` to ``FabricCapacity(...)`` — but fixing that requires
``Microsoft.Fabric/capacities`` write authority, which is ``fabric.py``'s own stage-scoped ``Fabric
Administrator`` grant, not this one. Flagged for that stage, not fixed here under a permission grant
that cannot reach it.

**Idempotence (FR-029):** a ``GET`` before the write, same discipline as every other stage. An
existing diagnostic setting whose ``workspaceId`` matches the platform's own Log Analytics workspace
and whose ``logs``/``metrics`` already request ``allLogs``/``AllMetrics`` is converged —
``SKIPPED_CONVERGED``/``NO_OP``. Anything else (absent, or present with different settings from a
prior interrupted or superseded attempt) submits the same deterministic ``PUT`` — forward-fix falls
out of the API's own create-or-replace semantics, matching ``blueprint.yaml``'s own recovery path
("Missing diagnostic settings are added on retry").
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


class MonitoringStageError(Exception):
    """A domain-level failure this stage recognised by name — no pipeline run has ever been
    triggered for this subscription, or the run did not reach a terminal state within budget."""


class MonitoringStage:
    """Implements ``Stage`` for the blueprint's ``monitoring`` stage. See module docstring for
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
            raise MonitoringStageError(
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
            raise MonitoringStageError(
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
            raise MonitoringStageError(
                f"pipeline run {outcome.run_id} did not reach a terminal state within the "
                f"poll budget ({self._max_poll_attempts} attempts at "
                f"{self._poll_interval_seconds}s)"
            )

        return stage_outcome_for(
            outcome, resources_affected=(), idempotence_outcome=IdempotenceOutcome.NO_OP
        )
