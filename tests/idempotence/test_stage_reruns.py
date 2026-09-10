"""T059 / SC-009 — every blueprint stage re-run against converged state is a no-op.

These are dedicated, per-stage idempotence checks. Several unit tests already prove the same
outcomes inline; this file centralises the spec-mandated coverage and adds the shared assertion the
story cares about: no second resource mutation once the stage has converged.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from azure.core.credentials_async import AsyncTokenCredential
from tests.conftest import APPROVAL_ID, CORRELATION_ID, SUBSCRIPTION_ID, TENANT_ID

from groundwork_contracts.audit import AuthorityChain, IdempotenceOutcome, StageStatus
from groundwork_contracts.deployment import (
    Checkpoint,
    Deployment,
    DeploymentStatus,
    SubscriptionLease,
)
from groundwork_contracts.tenant import CustomerTenant, SubscriptionEntitlement
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.devops_environments import environment_name, variable_group_name
from groundwork_orchestrator.stages.devops_pipelines import pipeline_name, rollback_pipeline_name
from groundwork_orchestrator.stages.devops_project import (
    DevOpsProjectStage,
    deployment_project_name,
)
from groundwork_orchestrator.stages.fabric import FabricStage, capacity_name, workspace_name
from groundwork_orchestrator.stages.identity import IdentityStage, service_connection_name
from groundwork_orchestrator.stages.infrastructure import (
    InfrastructureStage,
    deployment_resource_group_name,
)
from groundwork_orchestrator.stages.monitoring import MonitoringStage
from groundwork_orchestrator.stages.networking import NetworkingStage
from groundwork_orchestrator.stages.validation_tests import ValidationTestsStage
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint

pytestmark = pytest.mark.idempotence

ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = deployment_project_name(SUBSCRIPTION_ID)
PIPELINE_ID = 42
RUN_ID = 777
BOOTSTRAP_CLIENT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"
CAPACITY_ADMIN_UPN = "fabric-admin@customer.example"
WHAT_IF_URI = "https://example.invalid/whatif/existing.json"
NOW = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def blueprint():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


class _FakeCredential(AsyncTokenCredential):
    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> _FakeCredential:
        return self

    async def __aexit__(
        self,
        exc_type: object | None = None,
        exc: object | None = None,
        tb: object | None = None,
    ) -> None:
        return None


class _FakeContainer:
    def __init__(self, tenant: CustomerTenant) -> None:
        self._tenant = tenant.model_dump(mode="json") | {
            "id": tenant.tenant_id,
            "tenantId": tenant.tenant_id,
        }

    async def create_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self._tenant = body
        return body

    async def upsert_item(self, body: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        self._tenant = body
        return body

    async def read_item(self, item: str, partition_key: Any, **kwargs: Any) -> dict[str, Any]:
        return self._tenant

    async def query_items(
        self,
        query: str,
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **kwargs: Any,
    ):
        yield self._tenant


def _tenant_repository() -> TenantScopedRepository[CustomerTenant]:
    entitlement = SubscriptionEntitlement(
        subscription_id=SUBSCRIPTION_ID,
        display_name="test subscription",
        may_deploy=True,
        bootstrap_identity_resource_id=(
            f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
            f"{deployment_resource_group_name(SUBSCRIPTION_ID)}/providers/"
            f"Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-x"
        ),
        bootstrap_identity_client_id=BOOTSTRAP_CLIENT_ID,
        bootstrap_identity_created_at=NOW,
    )
    tenant = CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="test tenant",
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        subscriptions=(entitlement,),
        devops_organization_url=ORG_URL,
        fabric_capacity_admin_upn=CAPACITY_ADMIN_UPN,
    )
    return TenantScopedRepository(
        _FakeContainer(tenant), model_cls=CustomerTenant, id_field="tenant_id"
    )


def _deployment(*, checkpoint_stage: str | None = None, checkpoint_token: str = "") -> Deployment:
    checkpoint = (
        Checkpoint(stage_name=checkpoint_stage, resume_token=checkpoint_token, recorded_at=NOW)
        if checkpoint_stage is not None
        else None
    )
    return Deployment(
        deployment_id="88888888-8888-8888-8888-888888888888",
        tenant_id=TENANT_ID,
        subscription_id=SUBSCRIPTION_ID,
        correlation_id=CORRELATION_ID,
        authority=AuthorityChain(plan_hash=f"sha256:{'a' * 64}", approval_id=APPROVAL_ID),
        status=DeploymentStatus.EXECUTING,
        started_at=NOW,
        lease=SubscriptionLease(
            holder="88888888-8888-8888-8888-888888888888",
            expires_at=NOW + timedelta(hours=1),
        ),
        checkpoint=checkpoint,
        what_if_artefact_uri=WHAT_IF_URI,
    )


def _context(
    valid_plan,
    *,
    checkpoint_stage: str | None = None,
    checkpoint_token: str = "",
) -> StageExecutionContext:
    return StageExecutionContext(
        deployment=_deployment(
            checkpoint_stage=checkpoint_stage, checkpoint_token=checkpoint_token
        ),
        plan=valid_plan,
        credential=_FakeCredential(),
        attempt=2,
        devops_organization_url=ORG_URL,
        fabric_capacity_admin_upn=CAPACITY_ADMIN_UPN,
    )


def _run_list_response(
    *, state: str = "completed", result: str | None = "succeeded"
) -> httpx.Response:
    return httpx.Response(200, json={"value": [{"id": RUN_ID, "state": state, "result": result}]})


def _run_get_response(
    *, state: str = "completed", result: str | None = "succeeded"
) -> httpx.Response:
    return httpx.Response(200, json={"id": RUN_ID, "state": state, "result": result})


def _pipeline_list_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={"value": [{"id": PIPELINE_ID, "name": f"{PROJECT_NAME}-platform-release"}]},
    )


def _assert_successful_noop(outcome: StageOutcome) -> None:
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.NO_OP


async def test_devops_project_rerun_is_noop_with_zero_mutating_calls(valid_plan) -> None:
    calls = {
        "push": 0,
        "pipeline_create": 0,
        "var_group_create": 0,
        "environment_create": 0,
        "service_connection_create": 0,
        "pipeline_permissions_patch": 0,
        "fic_put": 0,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": [{"id": "proj-1", "name": PROJECT_NAME}]})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "repo-1", "name": PROJECT_NAME, "defaultBranch": "refs/heads/main"}
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/pushes"):
            calls["push"] += 1
            raise AssertionError("must not push on converged rerun")
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": 1, "name": pipeline_name(PROJECT_NAME)},
                        {"id": 2, "name": rollback_pipeline_name(PROJECT_NAME)},
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/_apis/pipelines"):
            calls["pipeline_create"] += 1
            raise AssertionError("must not create pipeline on converged rerun")
        if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 42,
                            "name": variable_group_name(PROJECT_NAME),
                            "variables": {
                                "resourceGroupName": {
                                    "value": deployment_resource_group_name(SUBSCRIPTION_ID),
                                    "isSecret": False,
                                },
                                "fabricCapacityName": {
                                    "value": capacity_name(SUBSCRIPTION_ID),
                                    "isSecret": False,
                                },
                                "fabricWorkspaceName": {
                                    "value": workspace_name(SUBSCRIPTION_ID),
                                    "isSecret": False,
                                },
                            },
                        }
                    ]
                },
            )
        if request.method == "POST" and path.endswith("/distributedtask/variablegroups"):
            calls["var_group_create"] += 1
            raise AssertionError("must not create variable group on converged rerun")
        if request.method == "GET" and path.endswith("/distributedtask/environments"):
            return httpx.Response(
                200,
                json={"value": [{"id": 7, "name": environment_name(PROJECT_NAME)}]},
            )
        if request.method == "POST" and path.endswith("/distributedtask/environments"):
            calls["environment_create"] += 1
            raise AssertionError("must not create environment on converged rerun")
        if request.method == "GET" and path.endswith("/_apis/serviceendpoint/endpoints"):
            endpoint = {
                "id": "conn-1",
                "name": service_connection_name(SUBSCRIPTION_ID),
                "type": "azurerm",
                "authorization": {
                    "scheme": "WorkloadIdentityFederation",
                    "parameters": {
                        "serviceprincipalid": BOOTSTRAP_CLIENT_ID,
                        "tenantid": TENANT_ID,
                        "workloadIdentityFederationIssuer": (
                            f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"
                        ),
                        "workloadIdentityFederationSubject": (
                            "/eid1/c/pub/t/FAKE/a/RkFLRQ/sc/org-guid/conn-1"
                        ),
                    },
                },
                "data": {"scopeLevel": "Subscription", "subscriptionId": SUBSCRIPTION_ID},
            }
            return httpx.Response(200, json={"value": [endpoint]})
        if request.method == "POST" and path.endswith("/_apis/serviceendpoint/endpoints"):
            calls["service_connection_create"] += 1
            raise AssertionError("must not create service connection on converged rerun")
        if request.method == "GET" and "/_apis/pipelines/pipelinepermissions/" in path:
            return httpx.Response(
                200,
                json={
                    "pipelines": [
                        {"id": 1, "authorized": True},
                        {"id": 2, "authorized": True},
                    ]
                },
            )
        if request.method == "PATCH" and "/_apis/pipelines/pipelinepermissions/" in path:
            calls["pipeline_permissions_patch"] += 1
            raise AssertionError("must not patch pipeline permissions on converged rerun")
        if request.url.host == "management.azure.com" and "federatedIdentityCredentials" in path:
            if request.method == "GET":
                return httpx.Response(
                    200,
                    json={
                        "properties": {
                            "issuer": f"https://login.microsoftonline.com/{TENANT_ID}/v2.0",
                            "subject": "/eid1/c/pub/t/FAKE/a/RkFLRQ/sc/org-guid/conn-1",
                            "audiences": ["api://AzureADTokenExchange"],
                        }
                    },
                )
            if request.method == "PUT":
                calls["fic_put"] += 1
                raise AssertionError("must not update FIC on converged rerun")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    ensure_sc_fic_calls = {"count": 0}

    async def no_op_fic(*args: Any, **kwargs: Any) -> bool:
        ensure_sc_fic_calls["count"] += 1
        return False

    stage = DevOpsProjectStage(
        customer_tenant_repository=_tenant_repository(),
        http_client=http_client,
        ensure_sc_fic=no_op_fic,
    )
    outcome = await stage.execute(_context(valid_plan))
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert all(count == 0 for count in calls.values())
    assert ensure_sc_fic_calls["count"] == 1


async def test_infrastructure_rerun_repolls_existing_run_without_triggering(valid_plan) -> None:
    calls = {"pipeline_trigger": 0, "pipeline_poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipeline_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            calls["pipeline_trigger"] += 1
            raise AssertionError("must not trigger a second pipeline run on converged rerun")
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            calls["pipeline_poll"] += 1
            return _run_get_response()
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = InfrastructureStage(http_client=http_client, poll_interval_seconds=0.0)
    outcome = await stage.execute(
        _context(
            valid_plan, checkpoint_stage="infrastructure", checkpoint_token=f"ado-run:{RUN_ID}"
        )
    )
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls == {"pipeline_trigger": 0, "pipeline_poll": 1}


async def test_identity_rerun_is_noop_and_never_triggers_new_write(valid_plan) -> None:
    calls = {"run_list": 0, "run_poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipeline_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            calls["run_list"] += 1
            return _run_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs/{RUN_ID}"):
            calls["run_poll"] += 1
            return _run_get_response()
        if request.method == "POST":
            raise AssertionError("verification-only stage must never write")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = IdentityStage(http_client=http_client, poll_interval_seconds=0.0)
    outcome = await stage.execute(_context(valid_plan))
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls["run_list"] == 1
    assert calls["run_poll"] == 0


async def test_networking_rerun_is_noop_and_never_triggers_new_write(valid_plan) -> None:
    calls = {"run_list": 0, "run_poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipeline_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            calls["run_list"] += 1
            return _run_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs/{RUN_ID}"):
            calls["run_poll"] += 1
            return _run_get_response()
        if request.method == "POST":
            raise AssertionError("verification-only stage must never write")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = NetworkingStage(http_client=http_client, poll_interval_seconds=0.0)
    outcome = await stage.execute(_context(valid_plan))
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls["run_list"] == 1
    assert calls["run_poll"] == 0


async def test_fabric_rerun_repolls_existing_run_without_triggering(valid_plan) -> None:
    calls = {"pipeline_trigger": 0, "pipeline_poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipeline_list_response()
        if request.method == "POST" and f"/pipelines/{PIPELINE_ID}/runs" in path:
            calls["pipeline_trigger"] += 1
            raise AssertionError("must not trigger a second pipeline run on converged rerun")
        if request.method == "GET" and f"/runs/{RUN_ID}" in path:
            calls["pipeline_poll"] += 1
            return _run_get_response()
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = FabricStage(http_client=http_client, poll_interval_seconds=0.0)
    outcome = await stage.execute(
        _context(valid_plan, checkpoint_stage="fabric", checkpoint_token=f"ado-run:{RUN_ID}")
    )
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls == {"pipeline_trigger": 0, "pipeline_poll": 1}


async def test_monitoring_rerun_is_noop_and_never_triggers_new_write(valid_plan) -> None:
    calls = {"run_list": 0, "run_poll": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/pipelines"):
            return _pipeline_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs"):
            calls["run_list"] += 1
            return _run_list_response()
        if request.method == "GET" and path.endswith(f"/pipelines/{PIPELINE_ID}/runs/{RUN_ID}"):
            calls["run_poll"] += 1
            return _run_get_response()
        if request.method == "POST":
            raise AssertionError("verification-only stage must never write")
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = MonitoringStage(http_client=http_client, poll_interval_seconds=0.0)
    outcome = await stage.execute(_context(valid_plan))
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls["run_list"] == 1
    assert calls["run_poll"] == 0


async def test_validation_tests_rerun_is_noop_and_read_only(valid_plan) -> None:
    calls = {"stack_get": 0, "workspace_get": 0, "capacity_get": 0}

    async def fake_get_capacity(
        credential: Any, subscription_id: str, resource_group: str, cap_name: str
    ) -> Any:
        calls["capacity_get"] += 1
        assert subscription_id == SUBSCRIPTION_ID
        assert resource_group == deployment_resource_group_name(SUBSCRIPTION_ID)
        assert cap_name == capacity_name(SUBSCRIPTION_ID)
        return type(
            "_Capacity",
            (),
            {"properties": type("_Props", (), {"state": "Active"})()},
        )()

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method != "GET":
            raise AssertionError("validation_tests is read-only and must never write")
        if "deploymentStacks" in url:
            calls["stack_get"] += 1
            return httpx.Response(
                200,
                json={
                    "name": "stack-groundwork-33333333",
                    "properties": {"provisioningState": "succeeded"},
                },
            )
        if url.endswith("/workspaces"):
            calls["workspace_get"] += 1
            return httpx.Response(
                200,
                json={"value": [{"displayName": workspace_name(SUBSCRIPTION_ID), "id": "ws-1"}]},
            )
        raise AssertionError(f"unexpected request: {request.method} {url}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = ValidationTestsStage(http_client=http_client, get_capacity=fake_get_capacity)
    outcome = await stage.execute(_context(valid_plan))
    await http_client.aclose()

    _assert_successful_noop(outcome)
    assert calls == {"stack_get": 1, "workspace_get": 1, "capacity_get": 1}


def test_stage_order_matches_real_blueprint_manifest() -> None:
    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    text = manifest.read_text(encoding="utf-8")
    positions = [
        text.index(f"- name: {name}")
        for name in (
            "devops_project",
            "infrastructure",
            "networking",
            "identity",
            "fabric",
            "monitoring",
            "validation_tests",
        )
    ]
    assert positions == sorted(positions)
