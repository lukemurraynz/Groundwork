"""T076b — variable-group and deployment-environment configuration for the Azure DevOps project
``devops_project.py`` (T076) already creates.

**Not a Sequencer-visible stage** — see ``devops_project.py``'s and ``devops_pipelines.py``'s own
docstrings for why: ``blueprint.yaml`` declares exactly one Azure-DevOps-related stage
(``devops_project``). This module is a plain helper invoked from ``DevOpsProjectStage.execute()``,
never registered with the ``Sequencer``. Unlike ``devops_pipelines.py``'s service-connection half,
nothing here has a stage-ordering dependency — every value this module writes is a deterministic
name computed from the subscription id alone, not a live property of a resource another stage
creates — so both pieces below are fully wired into ``devops_project.py``'s own execution.

Verified 2026-08-01 against ``learn.microsoft.com/rest/api/azure/devops`` (api-version 7.1, the same
host ``devops_project.py`` already established as reliable for this surface):

- Variable groups are organisation-scoped for writes (``POST``/``PUT
  {organization}/_apis/distributedtask/variablegroups``) but project-scoped for reads (``GET
  {organization}/{project}/_apis/distributedtask/variablegroups?groupName={name}``) — the same
  asymmetry the service-connection surface has (``devops_pipelines.py``), and for the same reason: a
  variable group is a first-class organisation object shared into projects via
  ``variableGroupProjectReferences``, not owned outright by one project.
- Deployment environments are project-scoped end to end (``POST``/``GET``
  ``{organization}/{project}/_apis/distributedtask/environments``) — no project-reference
  indirection needed, confirmed against ``microsoft/azure-devops-node-api``'s own
  ``EnvironmentCreateParameter`` interface (``{description?, name?}`` — nothing else is required to
  create one; VM/Kubernetes *resources* attached to an environment are a separate, later concern
  this platform does not need for Release 1).
- ``VariableValue`` (``microsoft/azure-devops-node-api``'s ``TaskAgentInterfaces.ts``, cross-checked
  against Learn's own definition table) carries an explicit ``isSecret`` flag. T076b's own task text
  is explicit that no secret values are written here — every variable this module writes sets
  ``isSecret: false`` explicitly, not merely by omission, and a found-but-drifted-to-secret variable
  is treated as non-converged and re-written back to ``false`` on the next run, so the "no secret
  values" invariant self-heals rather than merely holding at creation time.

**What content is actually appropriate here — decided, not invented.** The variable group carries
only names this codebase already computes deterministically and treats as non-sensitive elsewhere —
the deployment resource group name (``infrastructure.deployment_resource_group_name``), the Fabric
capacity name (``fabric.capacity_name``), and the Fabric workspace name (``fabric.workspace_name``).
All three are pure functions of ``subscription_id`` — calling them here, before the
``infrastructure``/``fabric`` stages that actually create those resources have run, is safe (no live
Azure read is involved, only string construction) and gives a customer's own pipeline authors a
stable, correct place to find these names without re-deriving the naming convention themselves.

The one deployment environment this module creates is named after the project itself
(``project_name``, the same deterministic value throughout this stage) with no VM/Kubernetes
resource attached — attaching resources to it is future scope this module does not invent.
"""

from __future__ import annotations

from typing import Any

import httpx

from groundwork_orchestrator.stages.fabric import capacity_name, workspace_name
from groundwork_orchestrator.stages.infrastructure import deployment_resource_group_name

API_VERSION = "7.1"
VARIABLE_GROUP_TYPE = "Vsts"


class DevOpsEnvironmentsConfigurationError(Exception):
    """A domain-level failure this module recognised by name — not an HTTP error, not a bug here."""


def variable_group_name(project_name: str) -> str:
    return f"{project_name}-platform"


def environment_name(project_name: str) -> str:
    return project_name


class DevOpsEnvironmentsConfigurer:
    """Helper invoked from ``DevOpsProjectStage.execute()`` — not a ``Stage`` itself."""

    def __init__(self, *, http_client: httpx.AsyncClient) -> None:
        self._client = http_client

    # ------------------------------------------------------------------
    # Variable group
    # ------------------------------------------------------------------

    async def ensure_variable_group(
        self,
        *,
        organization_url: str,
        project_id: str,
        project_name: str,
        subscription_id: str,
        headers: dict[str, str],
    ) -> tuple[bool, str]:
        name = variable_group_name(project_name)
        expected_variables = {
            "resourceGroupName": deployment_resource_group_name(subscription_id),
            "fabricCapacityName": capacity_name(subscription_id),
            "fabricWorkspaceName": workspace_name(subscription_id),
        }
        description = "Groundwork-managed platform resource names. No secret values (FR-049)."

        existing = await self._find_variable_group(organization_url, project_name, name, headers)
        body = self._variable_group_body(
            name, description, project_id, project_name, expected_variables
        )

        if existing is not None:
            if self._variables_match(existing, expected_variables):
                return False, str(existing["id"])
            url = (
                f"{organization_url}/_apis/distributedtask/variablegroups/{existing['id']}"
                f"?api-version={API_VERSION}"
            )
            response = await self._client.put(url, headers=headers, json=body)
            response.raise_for_status()
            return True, str(existing["id"])

        url = f"{organization_url}/_apis/distributedtask/variablegroups?api-version={API_VERSION}"
        response = await self._client.post(url, headers=headers, json=body)
        response.raise_for_status()
        created = response.json()
        return True, str(created["id"])

    @staticmethod
    def _variable_group_body(
        name: str,
        description: str,
        project_id: str,
        project_name: str,
        variables: dict[str, str],
    ) -> dict[str, Any]:
        return {
            "name": name,
            "description": description,
            "type": VARIABLE_GROUP_TYPE,
            "variables": {
                key: {"value": value, "isSecret": False} for key, value in variables.items()
            },
            "variableGroupProjectReferences": [
                {
                    "name": name,
                    "description": description,
                    "projectReference": {"id": project_id, "name": project_name},
                }
            ],
        }

    @staticmethod
    def _variables_match(existing: dict[str, Any], expected: dict[str, str]) -> bool:
        existing_variables = existing.get("variables") or {}
        for key, value in expected.items():
            entry = existing_variables.get(key)
            if entry is None or entry.get("value") != value or entry.get("isSecret", False):
                return False
        return True

    async def _find_variable_group(
        self, organization_url: str, project_name: str, name: str, headers: dict[str, str]
    ) -> dict[str, Any] | None:
        response = await self._client.get(
            f"{organization_url}/{project_name}/_apis/distributedtask/variablegroups",
            headers=headers,
            params={"groupName": name, "api-version": API_VERSION},
        )
        response.raise_for_status()
        for group in response.json()["value"]:
            if group["name"] == name:
                result: dict[str, Any] = group
                return result
        return None

    # ------------------------------------------------------------------
    # Deployment environment
    # ------------------------------------------------------------------

    async def ensure_environment(
        self,
        *,
        organization_url: str,
        project_name: str,
        headers: dict[str, str],
    ) -> tuple[bool, str, str]:
        """Idempotent create-if-absent. Returns ``(applied, name, environment_id)`` — the id is
        the resource key pipeline-permissions grants are keyed on. No update-in-place is
        attempted — an environment is just a named container (see module docstring); there is
        nothing to converge beyond its existence."""
        name = environment_name(project_name)
        existing = await self._find_environment(organization_url, project_name, name, headers)
        if existing is not None:
            return False, name, str(existing["id"])

        response = await self._client.post(
            f"{organization_url}/{project_name}/_apis/distributedtask/environments",
            headers=headers,
            params={"api-version": API_VERSION},
            json={
                "name": name,
                "description": (
                    "Groundwork-managed deployment environment for the platform release."
                ),
            },
        )
        response.raise_for_status()
        created = response.json()
        return True, name, str(created["id"])

    async def _find_environment(
        self, organization_url: str, project_name: str, name: str, headers: dict[str, str]
    ) -> dict[str, Any] | None:
        response = await self._client.get(
            f"{organization_url}/{project_name}/_apis/distributedtask/environments",
            headers=headers,
            params={"name": name, "api-version": API_VERSION},
        )
        response.raise_for_status()
        for env in response.json()["value"]:
            if env["name"] == name:
                result: dict[str, Any] = env
                return result
        return None
