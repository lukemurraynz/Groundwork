"""T076b — variable-group and deployment-environment configuration.

``httpx.MockTransport`` stands in for the network, the same convention as
``test_devops_project_stage.py``.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from groundwork_orchestrator.stages.devops_environments import (
    DevOpsEnvironmentsConfigurer,
    environment_name,
    variable_group_name,
)
from groundwork_orchestrator.stages.fabric import capacity_name, workspace_name
from groundwork_orchestrator.stages.infrastructure import deployment_resource_group_name

ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = "groundwork-33333333"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


def _expected_variables() -> dict[str, str]:
    return {
        "resourceGroupName": deployment_resource_group_name(SUBSCRIPTION_ID),
        "fabricCapacityName": capacity_name(SUBSCRIPTION_ID),
        "fabricWorkspaceName": workspace_name(SUBSCRIPTION_ID),
    }


# ---------------------------------------------------------------------------
# Variable group
# ---------------------------------------------------------------------------


def test_variable_group_name_is_deterministic() -> None:
    assert variable_group_name(PROJECT_NAME) == f"{PROJECT_NAME}-platform"


async def test_ensure_variable_group_creates_when_absent() -> None:
    created_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path.endswith("/distributedtask/variablegroups"):
            created_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": 42, **created_body})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, group_id = await configurer.ensure_variable_group(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        headers={},
    )
    await client.aclose()

    assert applied is True
    assert group_id == "42"
    assert created_body["name"] == variable_group_name(PROJECT_NAME)
    assert created_body["type"] == "Vsts"
    expected = _expected_variables()
    for key, value in expected.items():
        assert created_body["variables"][key] == {"value": value, "isSecret": False}
    # No secret values anywhere in the body at all (FR-049 / T076b).
    assert all(v["isSecret"] is False for v in created_body["variables"].values())


async def test_ensure_variable_group_no_op_when_converged() -> None:
    expected = _expected_variables()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 42,
                            "name": variable_group_name(PROJECT_NAME),
                            "variables": {
                                k: {"value": v, "isSecret": False} for k, v in expected.items()
                            },
                        }
                    ]
                },
            )
        if request.method in ("POST", "PUT") and "/distributedtask/variablegroups" in path:
            raise AssertionError("must not write when the variable group already matches")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, group_id = await configurer.ensure_variable_group(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        headers={},
    )
    await client.aclose()

    assert applied is False
    assert group_id == "42"


async def test_ensure_variable_group_forward_fixes_drifted_value() -> None:
    expected = _expected_variables()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
            drifted = dict(expected)
            drifted["resourceGroupName"] = "stale-value"
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 42,
                            "name": variable_group_name(PROJECT_NAME),
                            "variables": {
                                k: {"value": v, "isSecret": False} for k, v in drifted.items()
                            },
                        }
                    ]
                },
            )
        if request.method == "PUT" and "/distributedtask/variablegroups/42" in path:
            return httpx.Response(200, json={"id": 42})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, group_id = await configurer.ensure_variable_group(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        headers={},
    )
    await client.aclose()

    assert applied is True
    assert group_id == "42"


async def test_ensure_variable_group_forward_fixes_when_marked_secret() -> None:
    """A variable that somehow became isSecret=True must be treated as drifted, not converged —
    T076b's own "no secret values" requirement must self-heal, not just hold at creation time."""
    expected = _expected_variables()

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
            variables = {k: {"value": v, "isSecret": False} for k, v in expected.items()}
            variables["resourceGroupName"]["isSecret"] = True
            group = {"id": 42, "name": variable_group_name(PROJECT_NAME), "variables": variables}
            return httpx.Response(200, json={"value": [group]})
        if request.method == "PUT" and "/distributedtask/variablegroups/42" in path:
            return httpx.Response(200, json={"id": 42})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, _ = await configurer.ensure_variable_group(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        headers={},
    )
    await client.aclose()

    assert applied is True


# ---------------------------------------------------------------------------
# Deployment environment
# ---------------------------------------------------------------------------


def test_environment_name_is_the_project_name() -> None:
    assert environment_name(PROJECT_NAME) == PROJECT_NAME


async def test_ensure_environment_creates_when_absent() -> None:
    created_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/environments"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path.endswith("/distributedtask/environments"):
            created_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": 7, **created_body})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, name, _environment_id = await configurer.ensure_environment(
        organization_url=ORG_URL, project_name=PROJECT_NAME, headers={}
    )
    await client.aclose()

    assert applied is True
    assert name == PROJECT_NAME
    assert created_body["name"] == PROJECT_NAME


async def test_ensure_environment_no_op_when_already_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/distributedtask/environments"):
            return httpx.Response(200, json={"value": [{"id": 7, "name": PROJECT_NAME}]})
        if request.method == "POST" and path.endswith("/distributedtask/environments"):
            raise AssertionError("must not create an environment that already exists")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsEnvironmentsConfigurer(http_client=client)

    applied, name, _environment_id = await configurer.ensure_environment(
        organization_url=ORG_URL, project_name=PROJECT_NAME, headers={}
    )
    await client.aclose()

    assert applied is False
    assert name == PROJECT_NAME
