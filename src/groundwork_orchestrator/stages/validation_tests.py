"""The ``validation_tests`` stage (T083; FR-034): read-only post-deploy health checks against
everything the prior six stages just deployed. This is the blueprint's last stage — nothing after it
writes anything, so its own ``idempotenceContract`` ("Read-only... re-running is always safe") holds
by construction, not by convergence logic like every earlier stage.

**Why this cannot reuse ``groundwork_controlplane.validation.engine.ReadinessEngine``, despite the
obvious shape match.** ``ReadinessEngine`` already solves exactly this problem — a registered set of
check functions, a context carrying a scoped credential, a raised check turned into a non-passing
outcome rather than a silent pass — for the pre-approval readiness path (User Story 1). Importing it
here would be the fastest way to build this stage. It is also structurally forbidden:
``groundwork_orchestrator`` must never import ``groundwork_controlplane`` (the
deterministic-execution boundary, enforced by ``tests/unit/test_import_boundaries.py``, not a
style preference). This module instead re-implements
the same *shape* — independent checks, a raise-becomes-failure guarantee — using only what
``groundwork_orchestrator``/``groundwork_contracts``/``groundwork_shared`` already provide, the same
boundary every other stage in this package already respects.

**How FR-034 is actually satisfied here, without new machinery.** FR-034: "a validation test that
did not execute is reported as failed, never as passed." ``Sequencer._run_one_stage`` (see
``engine/sequencer.py``'s own docstring, point 4) already guarantees that *any* exception a
``Stage.execute`` raises — a network error, an unexpected response shape, a real assertion failure —
converts to a halted ``FAILED`` outcome with the error recorded, never a silent drop. This stage
therefore does not wrap each check in its own ``try``/``except`` the way ``ReadinessEngine`` does
(that engine needs one because *one* raised assertion must not stop it from evaluating the rest of
the contract for a single combined report); a deployment stage has no such requirement — the very
first check that cannot execute, or does not pass, is exactly where a validation-test stage should
stop and halt, per this stage's own ``recoveryPath`` ("Retry... a retry budget exhaustion halts the
deployment rather than completing it"). Each check below either returns cleanly (it ran and passed)
or raises (it either could not run or ran and failed) — both cases now correctly become the stage's
single ``FAILED`` outcome via the sequencer's own existing guarantee, with no risk of a check
silently being skipped and treated as passed.

**The three checks, and why these three.** Each independently re-verifies a claim an earlier stage
already made, using a live read against the real deployed platform rather than trusting that stage's
own reported success:

1. **Deployment stack ``provisioningState == "succeeded"``** (``infrastructure.py``'s own stack,
   re-``GET``, same URL shape ``networking.py`` already reads). Re-checked rather than assumed: a
   deployment resumed from a checkpoint after this stage's own retry budget, or one whose earlier
   stage outcome was itself never durably confirmed, should not pass validation on trust alone.
2. **Fabric capacity ``properties.state == "Active"``** — deliberately not ``provisioningState ==
   "Succeeded"`` (which ``fabric.py`` already checked once, at creation time). ``state`` is the
   separate, live-operational field the ``azure-mgmt-fabric`` SDK's own ``FabricCapacityProperties``
   model exposes ("more states outside of resource provisioning": Active, Paused, Suspended, Failed,
   ...) — a capacity can finish provisioning successfully and later end up
   ``Paused``/``Suspended``/``Failed`` before this stage ever runs, which the fabric stage's own
   one-time creation check cannot see.
3. **Fabric workspace reachable via the Fabric REST API** (``GET /v1/workspaces``, matched by
   ``displayName`` — the same call and matching convention ``fabric.py``'s own ``_find_workspace``
   already uses). This is deliberately the customer-facing API surface itself, not a second ARM
   read: FR-034's "validation tests... against the deployed platform" is about whether the platform
   a customer will actually use is reachable, not just whether Azure's control plane once returned
   201 for it.

**What this stage deliberately does not re-check, and why.** The Key Vault's private endpoint
connection state is already independently verified by ``networking.py`` (T079), a separate earlier
stage whose entire job is exactly that; duplicating it here would be the same busywork
``networking.py`` avoided for its own connection to ``infrastructure.py``. The Fabric managed
private endpoint and the Fabric-side capacity-ID resolution are similarly already exercised as a
precondition of ``fabric.py`` having reached ``SUCCEEDED`` in the first place — this stage checks
the platform's current live state, not whether every earlier stage's own write path already worked
(that is what those stages' own outcomes already recorded). The ``monitoring`` stage's diagnostic
setting is not re-verified here either: it is this stage's immediate predecessor, its own
``execute`` already performed a ``GET`` before writing, and a diagnostic setting's presence is
observability configuration, not part of "is the platform itself healthy" in the sense FR-034 is
aimed at.

**Idempotence:** always ``SKIPPED_CONVERGED``/``NO_OP`` on success, the same reasoning
``networking.py`` already applies to itself — there is nothing to converge toward, only live state
to confirm, and this stage's own contract says so explicitly.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx
from azure.mgmt.fabric.aio import FabricMgmtClient

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.fabric import (
    FABRIC_API_ENDPOINT,
    FABRIC_SCOPE,
    capacity_name,
    workspace_name,
)
from groundwork_orchestrator.stages.infrastructure import (
    API_VERSION as DEPLOYMENT_STACK_API_VERSION,
)
from groundwork_orchestrator.stages.infrastructure import (
    ARM_ENDPOINT,
    ARM_SCOPE,
    deployment_resource_group_name,
    deployment_stack_name,
)

_ACTIVE_STATE = "Active"

# (credential, subscription_id, resource_group, capacity_name) -> the SDK's FabricCapacity model
# (or anything duck-typed the same way — see _check_fabric_capacity).
_CapacityGetter = Callable[[Any, str, str, str], Awaitable[Any]]


class ValidationTestsStageError(Exception):
    """A domain-level failure this stage recognised by name — a post-deploy check ran and did not
    pass. Not caught here; ``Sequencer._run_one_stage`` converts it (and any other raised exception)
    into a halted, fully-recorded ``FAILED`` outcome, which is exactly how FR-034's "did not execute
    is reported as failed" guarantee is met — see this module's own docstring."""


async def _default_get_capacity(
    credential: Any, subscription_id: str, resource_group: str, cap_name: str
) -> Any:
    async with FabricMgmtClient(credential, subscription_id) as client:
        return await client.fabric_capacities.get(resource_group, cap_name)


class ValidationTestsStage:
    """Implements ``Stage`` for the blueprint's ``validation_tests`` stage."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        get_capacity: _CapacityGetter | None = None,
    ) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None
        # Injectable the same way fabric.py injects ensure_capacity — azure-core's async transport
        # pipeline is not httpx-compatible, so MockTransport cannot stand in for the SDK call
        # directly; testing the real Azure-calling glue at this seam instead avoids mocking the
        # SDK's pipeline internals.
        self._get_capacity = get_capacity or _default_get_capacity

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        subscription_id = context.plan.subscription_id
        resource_group = deployment_resource_group_name(subscription_id)

        stack_name = await self._check_deployment_stack(subscription_id, context.credential)
        cap_name = await self._check_fabric_capacity(
            context.credential, subscription_id, resource_group
        )
        ws_name = await self._check_fabric_workspace(subscription_id, context.credential)

        return StageOutcome(
            status=StageStatus.SUCCEEDED,
            idempotence_outcome=IdempotenceOutcome.NO_OP,
            resources_affected=(stack_name, cap_name, ws_name),
        )

    async def _check_deployment_stack(self, subscription_id: str, credential: Any) -> str:
        stack_name = deployment_stack_name(subscription_id)
        token = await credential.get_token(ARM_SCOPE)
        headers = {"Authorization": f"Bearer {token.token}"}
        url = (
            f"{ARM_ENDPOINT}/subscriptions/{subscription_id}/providers/Microsoft.Resources"
            f"/deploymentStacks/{stack_name}?api-version={DEPLOYMENT_STACK_API_VERSION}"
        )
        response = await self._client.get(url, headers=headers)
        if response.status_code == 404:
            raise ValidationTestsStageError(
                f"deployment stack {stack_name!r} does not exist; infrastructure was never deployed"
            )
        response.raise_for_status()
        stack: dict[str, Any] = response.json()
        state = stack["properties"]["provisioningState"]
        if state != "succeeded":
            raise ValidationTestsStageError(
                f"deployment stack {stack_name!r} has provisioningState {state!r}, not 'succeeded'"
            )
        return stack_name

    async def _check_fabric_capacity(
        self, credential: Any, subscription_id: str, resource_group: str
    ) -> str:
        cap_name = capacity_name(subscription_id)
        capacity = await self._get_capacity(credential, subscription_id, resource_group, cap_name)
        properties = capacity.properties
        state = properties.state if properties is not None else None
        if state != _ACTIVE_STATE:
            raise ValidationTestsStageError(
                f"Fabric capacity {cap_name!r} has state {state!r}, not {_ACTIVE_STATE!r}"
            )
        return cap_name

    async def _check_fabric_workspace(self, subscription_id: str, credential: Any) -> str:
        ws_name = workspace_name(subscription_id)
        token = await credential.get_token(FABRIC_SCOPE)
        headers = {"Authorization": f"Bearer {token.token}"}
        response = await self._client.get(f"{FABRIC_API_ENDPOINT}/workspaces", headers=headers)
        response.raise_for_status()
        for workspace in response.json().get("value", []):
            if workspace["displayName"] == ws_name:
                return ws_name
        raise ValidationTestsStageError(
            f"Fabric workspace {ws_name!r} was not found via the Fabric REST API; the fabric "
            f"stage may not have converged"
        )
