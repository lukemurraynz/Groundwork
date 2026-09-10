"""T076a — pipeline creation and (independently, not-yet-wired) service-connection creation.

``httpx.MockTransport`` stands in for the network, the same convention as
``test_devops_project_stage.py``. ``ensure_service_connection`` and
``resolve_managed_identity_client_id`` are tested directly here even though
``devops_project.py`` does not call them yet — see ``devops_pipelines.py``'s own docstring for the
real sequencing blocker that keeps them unwired.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from groundwork_orchestrator.stages.devops_pipelines import (
    DevOpsPipelinesConfigurer,
    pipeline_name,
)

ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = "groundwork-33333333"
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"


class _FakeCredential:
    def __init__(self, token: str = "fake-arm-token") -> None:  # noqa: S107
        self._token = token

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token: str

        t = _Token()
        t.token = self._token
        return t


# ---------------------------------------------------------------------------
# Pipeline creation — wired into devops_project.py.
# ---------------------------------------------------------------------------


def test_pipeline_name_is_deterministic() -> None:
    assert pipeline_name(PROJECT_NAME) == f"{PROJECT_NAME}-platform-release"


async def test_ensure_pipeline_creates_when_absent() -> None:
    created_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path.endswith("/_apis/pipelines"):
            created_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": 1, "name": created_body["name"]})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsPipelinesConfigurer(http_client=client)

    applied, name, _pipeline_id = await configurer.ensure_pipeline(
        organization_url=ORG_URL,
        project_name=PROJECT_NAME,
        repository_id="repo-1",
        repository_name=PROJECT_NAME,
        headers={},
    )
    await client.aclose()

    assert applied is True
    assert name == pipeline_name(PROJECT_NAME)
    assert created_body["configuration"]["type"] == "yaml"
    assert created_body["configuration"]["path"] == "/azure-pipelines.yml"
    assert created_body["configuration"]["repository"] == {
        "id": "repo-1",
        "name": PROJECT_NAME,
        "type": "azureReposGit",
    }


async def test_ensure_pipeline_no_op_when_already_present() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200, json={"value": [{"id": 1, "name": pipeline_name(PROJECT_NAME)}]}
            )
        if request.method == "POST" and path.endswith("/_apis/pipelines"):
            raise AssertionError("must not create a pipeline that already exists")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsPipelinesConfigurer(http_client=client)

    applied, name, _pipeline_id = await configurer.ensure_pipeline(
        organization_url=ORG_URL,
        project_name=PROJECT_NAME,
        repository_id="repo-1",
        repository_name=PROJECT_NAME,
        headers={},
    )
    await client.aclose()

    assert applied is False
    assert name == pipeline_name(PROJECT_NAME)


# ---------------------------------------------------------------------------
# Service connection — implemented, independently tested, not yet wired into devops_project.py.
# ---------------------------------------------------------------------------


def _service_connection(
    *,
    scheme: str = "WorkloadIdentityFederation",
    client_id: str = "client-id-1",
    tenant_id: str = "tenant-id-1",
    subscription_id: str = SUBSCRIPTION_ID,
) -> dict[str, Any]:
    return {
        "id": "sc-1",
        "name": PROJECT_NAME,
        "authorization": {
            "scheme": scheme,
            "parameters": {"serviceprincipalid": client_id, "tenantid": tenant_id},
        },
        "data": {"scopeLevel": "Subscription", "subscriptionId": subscription_id},
    }


async def test_ensure_service_connection_creates_when_absent() -> None:
    created_body: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/serviceendpoint/endpoints"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path.endswith("/serviceendpoint/endpoints"):
            created_body.update(json.loads(request.content))
            return httpx.Response(200, json={"id": "sc-new", **created_body})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsPipelinesConfigurer(http_client=client)

    applied, connection_id, _endpoint = await configurer.ensure_service_connection(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        connection_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        tenant_id="tenant-id-1",
        managed_identity_client_id="client-id-1",
        headers={},
    )
    await client.aclose()

    assert applied is True
    assert connection_id == "sc-new"
    assert created_body["type"] == "azurerm"
    assert created_body["authorization"]["scheme"] == "WorkloadIdentityFederation"
    assert created_body["authorization"]["parameters"]["serviceprincipalid"] == "client-id-1"
    assert created_body["authorization"]["parameters"]["tenantid"] == "tenant-id-1"
    assert created_body["data"]["creationMode"] == "Manual"
    assert created_body["data"]["scopeLevel"] == "Subscription"
    assert created_body["data"]["subscriptionId"] == SUBSCRIPTION_ID
    assert created_body["serviceEndpointProjectReferences"] == [
        {
            "name": PROJECT_NAME,
            "description": created_body["description"],
            "projectReference": {"id": "proj-1", "name": PROJECT_NAME},
        }
    ]


async def test_ensure_service_connection_no_op_when_converged() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/serviceendpoint/endpoints"):
            return httpx.Response(200, json={"value": [_service_connection()]})
        if request.method in ("POST", "PUT") and "/serviceendpoint/endpoints" in path:
            raise AssertionError("must not write when the service connection already matches")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsPipelinesConfigurer(http_client=client)

    applied, connection_id, _endpoint = await configurer.ensure_service_connection(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        connection_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        tenant_id="tenant-id-1",
        managed_identity_client_id="client-id-1",
        headers={},
    )
    await client.aclose()

    assert applied is False
    assert connection_id == "sc-1"


async def test_ensure_service_connection_forward_fixes_drifted_client_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/serviceendpoint/endpoints"):
            return httpx.Response(200, json={"value": [_service_connection(client_id="stale")]})
        if request.method == "PUT" and "/serviceendpoint/endpoints/sc-1" in path:
            return httpx.Response(200, json=_service_connection(client_id="client-id-1"))
        raise AssertionError(f"unexpected request: {request.method} {path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    configurer = DevOpsPipelinesConfigurer(http_client=client)

    applied, connection_id, _endpoint = await configurer.ensure_service_connection(
        organization_url=ORG_URL,
        project_id="proj-1",
        project_name=PROJECT_NAME,
        connection_name=PROJECT_NAME,
        subscription_id=SUBSCRIPTION_ID,
        tenant_id="tenant-id-1",
        managed_identity_client_id="client-id-1",
        headers={},
    )
    await client.aclose()

    assert applied is True
    assert connection_id == "sc-1"


# resolve_managed_identity_client_id() and its tests removed 2026-08-24 (Clarifications,
# FR-006a) — see devops_pipelines.py's own note at the old method's former location. The client
# id now comes from SubscriptionEntitlement.bootstrap_identity_client_id.


# --- federation_parameters (found live 2026-08-25) -------------------------------


def test_federation_parameters_extracts_entra_issuer_pair() -> None:
    endpoint = {
        "authorization": {
            "scheme": "WorkloadIdentityFederation",
            "parameters": {
                "tenantid": "t",
                "serviceprincipalid": "sp",
                "workloadIdentityFederationIssuer": (
                    "https://login.microsoftonline.com/tenant/v2.0"
                ),
                "workloadIdentityFederationSubject": "/eid1/c/pub/t/x/a/y/sc/org/endpoint",
            },
        }
    }
    from groundwork_orchestrator.stages.devops_pipelines import DevOpsPipelinesConfigurer

    assert DevOpsPipelinesConfigurer.federation_parameters(endpoint) == (
        "https://login.microsoftonline.com/tenant/v2.0",
        "/eid1/c/pub/t/x/a/y/sc/org/endpoint",
    )


def test_federation_parameters_returns_none_when_absent() -> None:
    from groundwork_orchestrator.stages.devops_pipelines import DevOpsPipelinesConfigurer

    assert (
        DevOpsPipelinesConfigurer.federation_parameters(
            {"authorization": {"parameters": {"serviceprincipalid": "sp"}}}
        )
        is None
    )
