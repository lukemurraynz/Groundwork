"""T076 — the ``devops_project`` stage's Azure DevOps project creation, platform-source push, and
(T076a pipeline half / T076b) pipeline, variable-group, and deployment-environment configuration.

``httpx.MockTransport`` stands in for the network, the same convention as
``test_retail_prices_client.py``. Each handler is a small state machine over call count, since one
``execute()`` call makes several real requests in sequence (list, maybe create, poll, list again,
list repositories, maybe push, then the T076a/T076b sub-resource list-then-maybe-write calls —
see ``_subresource_response`` below). ``devops_pipelines.py`` and ``devops_environments.py`` have
their own, more detailed unit tests (``test_devops_pipelines.py``, ``test_devops_environments.py``);
these tests only need to confirm the wiring — that ``execute()`` calls through to each, and that the
overall stage outcome reflects whether any of them actually applied a change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from groundwork_contracts.audit import IdempotenceOutcome, StageStatus
from groundwork_contracts.tenant import CustomerTenant, SubscriptionEntitlement
from groundwork_orchestrator.engine.sequencer import StageExecutionContext, StageOutcome
from groundwork_orchestrator.stages.devops_environments import (
    environment_name,
    variable_group_name,
)
from groundwork_orchestrator.stages.devops_pipelines import pipeline_name, rollback_pipeline_name
from groundwork_orchestrator.stages.devops_project import (
    DevOpsProjectStage,
    DevOpsProjectStageError,
    blueprint_source_hash,
    deployment_project_name,
)
from groundwork_orchestrator.stages.fabric import capacity_name, workspace_name
from groundwork_orchestrator.stages.identity import service_connection_name
from groundwork_orchestrator.stages.infrastructure import deployment_resource_group_name

ORG_URL = "https://dev.azure.com/example-org"
PROJECT_NAME = "groundwork-33333333"  # matches conftest.SUBSCRIPTION_ID's first 8 chars
SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
TENANT_ID = "11111111-1111-1111-1111-111111111111"
BOOTSTRAP_CLIENT_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


class _FakeTenantRepository:
    """Stands in for ``CustomerTenantRepository`` — returns a tenant whose ``SUBSCRIPTION_ID``
    entitlement carries a bootstrap identity client id, matching what ``api/tenants.py``'s
    ``bootstrap_identity`` route (FR-006a) would have already recorded before any deployment
    starts. ``bootstrapped=False`` mimics a subscription queued for deployment without ever
    completing that onboarding step."""

    def __init__(self, *, bootstrapped: bool = True) -> None:
        self._bootstrapped = bootstrapped

    async def read(self, _partition_key: str, _id: str) -> CustomerTenant:
        entitlement = SubscriptionEntitlement(
            subscription_id=SUBSCRIPTION_ID,
            display_name="test subscription",
            may_deploy=True,
            bootstrap_identity_resource_id=(
                f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/rg-groundwork-33333333"
                f"/providers/Microsoft.ManagedIdentity/userAssignedIdentities/uami-gw-x"
                if self._bootstrapped
                else None
            ),
            bootstrap_identity_client_id=BOOTSTRAP_CLIENT_ID if self._bootstrapped else None,
            bootstrap_identity_created_at=(
                datetime(2026, 7, 1, tzinfo=UTC) if self._bootstrapped else None
            ),
        )
        return CustomerTenant(
            tenant_id=TENANT_ID,
            display_name="test tenant",
            approved_regions=frozenset({"australiaeast"}),
            data_residency_regions=frozenset({"australiaeast"}),
            subscriptions=(entitlement,),
        )


def _stage(
    *,
    organization_url: str | None = ORG_URL,
    http_client: httpx.AsyncClient | None = None,
    poll_interval_seconds: float = 2.0,
    bootstrapped: bool = True,
    fic_applied: bool | None = None,
) -> DevOpsProjectStage:
    kwargs: dict[str, Any] = {
        "customer_tenant_repository": _FakeTenantRepository(bootstrapped=bootstrapped),
        "organization_url": organization_url,
        "http_client": http_client,
        "poll_interval_seconds": poll_interval_seconds,
    }
    if fic_applied is not None:

        async def fake_ensure_sc_fic(*args: Any, **kwargs_: Any) -> bool:
            return fic_applied

        kwargs["ensure_sc_fic"] = fake_ensure_sc_fic
    return DevOpsProjectStage(**kwargs)


class _FakeCredential:
    async def __aenter__(self) -> _FakeCredential:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: object | None = None,
    ) -> None:
        del exc_type, exc_value, traceback
        return

    async def close(self) -> None:
        return None

    async def get_token(self, *scopes: str, **kwargs: object) -> Any:
        class _Token:
            token = "fake-devops-token"  # noqa: S105

        return _Token()


def _project(name: str = PROJECT_NAME, project_id: str = "proj-1") -> dict[str, Any]:
    return {"id": project_id, "name": name}


def _repository(*, default_branch: str | None, repo_id: str = "repo-1") -> dict[str, Any]:
    repo: dict[str, Any] = {"id": repo_id, "name": PROJECT_NAME}
    if default_branch is not None:
        repo["defaultBranch"] = default_branch
    return repo


def _context(*, plan) -> StageExecutionContext:
    return StageExecutionContext(
        deployment=_deployment_stub(), plan=plan, credential=_FakeCredential(), attempt=1
    )


def _deployment_stub():
    from datetime import UTC, datetime, timedelta

    from groundwork_contracts.audit import AuthorityChain
    from groundwork_contracts.deployment import Deployment, DeploymentStatus, SubscriptionLease

    now = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)
    return Deployment(
        deployment_id="88888888-8888-8888-8888-888888888888",
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id="33333333-3333-3333-3333-333333333333",
        correlation_id="77777777-7777-7777-7777-777777777777",
        authority=AuthorityChain(
            plan_hash=f"sha256:{'a' * 64}",
            approval_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        ),
        status=DeploymentStatus.EXECUTING,
        started_at=now,
        lease=SubscriptionLease(
            holder="88888888-8888-8888-8888-888888888888", expires_at=now + timedelta(hours=1)
        ),
    )


def _converged_variables() -> dict[str, dict[str, Any]]:
    return {
        "resourceGroupName": {
            "value": deployment_resource_group_name(SUBSCRIPTION_ID),
            "isSecret": False,
        },
        "fabricCapacityName": {"value": capacity_name(SUBSCRIPTION_ID), "isSecret": False},
        "fabricWorkspaceName": {"value": workspace_name(SUBSCRIPTION_ID), "isSecret": False},
    }


def _subresource_response(request: httpx.Request, *, converged: bool) -> httpx.Response | None:
    """Handles the T076a (pipeline half) / T076b list-then-maybe-write calls every ``execute()``
    now makes after resolving the project and repository — shared across this file's tests since
    none of them are actually testing those sub-resources' own behaviour (see
    ``test_devops_pipelines.py``/``test_devops_environments.py`` for that). ``converged=True``
    reports every sub-resource as already present and matching, so the caller's own push-related
    assertions are unaffected by these calls. Returns ``None`` for anything it does not recognise,
    so the caller's own handler can fall through to its own ``AssertionError``.
    """
    path = request.url.path
    if request.method == "GET" and path.endswith("/_apis/pipelines"):
        if converged:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": 1, "name": pipeline_name(PROJECT_NAME)},
                        {"id": 2, "name": rollback_pipeline_name(PROJECT_NAME)},
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})
    if request.method == "POST" and path.endswith("/_apis/pipelines"):
        body = json.loads(request.content.decode("utf-8"))
        pipeline = body["name"]
        pipeline_id = 1 if pipeline == pipeline_name(PROJECT_NAME) else 2
        return httpx.Response(200, json={"id": pipeline_id, "name": pipeline})

    if request.method == "GET" and path.endswith("/distributedtask/variablegroups"):
        if converged:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "id": 42,
                            "name": variable_group_name(PROJECT_NAME),
                            "variables": _converged_variables(),
                        }
                    ]
                },
            )
        return httpx.Response(200, json={"value": []})
    if request.method == "POST" and path.endswith("/distributedtask/variablegroups"):
        return httpx.Response(200, json={"id": 42, "name": variable_group_name(PROJECT_NAME)})

    if request.method == "GET" and path.endswith("/distributedtask/environments"):
        if converged:
            env = {"id": 7, "name": environment_name(PROJECT_NAME)}
            return httpx.Response(200, json={"value": [env]})
        return httpx.Response(200, json={"value": []})
    if request.method == "POST" and path.endswith("/distributedtask/environments"):
        return httpx.Response(200, json={"id": 7, "name": environment_name(PROJECT_NAME)})

    if request.method == "GET" and path.endswith("/_apis/serviceendpoint/endpoints"):
        if converged:
            endpoint = {
                "id": "conn-1",
                "name": service_connection_name(SUBSCRIPTION_ID),
                "type": "azurerm",
                "authorization": {
                    "scheme": "WorkloadIdentityFederation",
                    "parameters": {
                        "serviceprincipalid": BOOTSTRAP_CLIENT_ID,
                        "tenantid": TENANT_ID,
                        # The Entra-issuer flavour ADO writes server-side at creation time
                        # (found live 2026-08-25); the stage mirrors these into a federated
                        # credential on the bootstrap identity.
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
        return httpx.Response(200, json={"value": []})
    if request.method == "POST" and path.endswith("/_apis/serviceendpoint/endpoints"):
        return httpx.Response(
            200, json={"id": "conn-1", "name": service_connection_name(SUBSCRIPTION_ID)}
        )

    # Pipeline-permissions reads/writes (protected-resource authorization).
    if request.method == "GET" and "/_apis/pipelines/pipelinepermissions/" in path:
        if converged:
            return httpx.Response(
                200,
                json={
                    "pipelines": [
                        {"id": 1, "authorized": True},
                        {"id": 2, "authorized": True},
                    ]
                },
            )
        return httpx.Response(200, json={"pipelines": []})
    if request.method == "PATCH" and "/_apis/pipelines/pipelinepermissions/" in path:
        body = json.loads(request.content.decode("utf-8"))
        return httpx.Response(200, json={"pipelines": body["pipelines"]})

    # ARM-side federated-credential child resource on the bootstrap identity (absolute
    # management.azure.com URL routed through the same mock transport). Converged means the
    # credential already exists with exactly the values the stage would write.
    if request.url.host == "management.azure.com" and "federatedIdentityCredentials" in path:
        if request.method == "GET":
            if converged:
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
            return httpx.Response(404, json={"error": {"code": "NotFound"}})
        if request.method == "PUT":
            return httpx.Response(200, json={"name": "fc-sc-conn-1"})
    if request.url.host == "management.azure.com":
        raise AssertionError(f"unexpected ARM request: {request.method} {path}")

    return None


def test_deployment_project_name_is_deterministic() -> None:
    assert deployment_project_name("33333333-3333-3333-3333-333333333333") == PROJECT_NAME


def test_blueprint_source_hash_is_deterministic_for_identical_tree_orderings(tmp_path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    for root in (first, second):
        (root / "b.txt").write_text("bravo", encoding="utf-8")
        nested = root / "nested"
        nested.mkdir()
        (nested / "a.txt").write_text("alpha", encoding="utf-8")

    assert blueprint_source_hash(first) == blueprint_source_hash(second)


async def test_creates_project_and_pushes_platform_source_when_nothing_exists(
    valid_plan,
) -> None:
    calls = {"list_projects": 0, "list_repos": 0, "operation_polls": 0}
    pushed_body: dict[str, Any] = {}
    created_pipelines: list[str] = []
    authorizations: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            calls["list_projects"] += 1
            if calls["list_projects"] == 1:
                return httpx.Response(200, json={"value": []})
            return httpx.Response(200, json={"value": [_project()]})
        if request.method == "POST" and path.endswith("/_apis/projects"):
            return httpx.Response(
                202,
                json={
                    "id": "op-1",
                    "status": "queued",
                    "url": f"{ORG_URL}/_apis/operations/op-1",
                },
            )
        if request.method == "GET" and "/_apis/operations/op-1" in path:
            calls["operation_polls"] += 1
            status = "queued" if calls["operation_polls"] == 1 else "succeeded"
            return httpx.Response(200, json={"id": "op-1", "status": status})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            calls["list_repos"] += 1
            return httpx.Response(200, json={"value": [_repository(default_branch=None)]})
        if request.method == "POST" and path.endswith("/pushes"):
            pushed_body.update(json.loads(request.content.decode("utf-8")))
            return httpx.Response(201, json={"pushId": 1})
        if request.method == "POST" and path.endswith("/_apis/pipelines"):
            body = json.loads(request.content.decode("utf-8"))
            created_pipelines.append(body["name"])
            return _subresource_response(request, converged=False) or httpx.Response(500)
        if request.method == "PATCH" and "/_apis/pipelines/pipelinepermissions/" in path:
            authorizations.append(json.loads(request.content.decode("utf-8")))
            return _subresource_response(request, converged=False) or httpx.Response(500)
        sub_response = _subresource_response(request, converged=False)
        if sub_response is not None:
            return sub_response
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client, poll_interval_seconds=0.0, fic_applied=True)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert isinstance(outcome, StageOutcome)
    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.APPLIED
    assert PROJECT_NAME in outcome.resources_affected
    assert "blueprint.yaml" in outcome.resources_affected
    assert "main.bicep" in outcome.resources_affected
    assert "rollback.yml" in outcome.resources_affected
    assert pipeline_name(PROJECT_NAME) in outcome.resources_affected
    assert rollback_pipeline_name(PROJECT_NAME) in outcome.resources_affected
    assert environment_name(PROJECT_NAME) in outcome.resources_affected
    assert "conn-1" in outcome.resources_affected
    assert calls["list_projects"] == 2
    assert calls["operation_polls"] == 2
    commit_message = pushed_body["commits"][0]["comment"]
    assert commit_message.startswith(
        "Groundwork: initial platform source (standard-production-fabric, sha256:"
    )
    assert created_pipelines == [pipeline_name(PROJECT_NAME), rollback_pipeline_name(PROJECT_NAME)]
    assert authorizations == [
        {"pipelines": [{"id": 1, "authorized": True}, {"id": 2, "authorized": True}]},
        {"pipelines": [{"id": 1, "authorized": True}, {"id": 2, "authorized": True}]},
    ]


async def test_no_op_when_repository_already_has_a_default_branch(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": [_project()]})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(
                200, json={"value": [_repository(default_branch="refs/heads/main")]}
            )
        if request.method == "POST" and path.endswith("/pushes"):
            raise AssertionError("must not push when the repository is already converged")
        sub_response = _subresource_response(request, converged=True)
        if sub_response is not None:
            return sub_response
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client, fic_applied=False)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.NO_OP
    assert outcome.resources_affected == (
        PROJECT_NAME,
        pipeline_name(PROJECT_NAME),
        rollback_pipeline_name(PROJECT_NAME),
        "42",
        environment_name(PROJECT_NAME),
        "conn-1",
    )


async def test_forward_fixes_when_project_exists_but_repository_is_still_empty(
    valid_plan,
) -> None:
    """A prior attempt may have created the project and then failed before pushing — the project
    existing must not be mistaken for the stage having already converged."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": [_project()]})
        if request.method == "POST" and path.endswith("/_apis/projects"):
            raise AssertionError("must not create a project that already exists")
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(200, json={"value": [_repository(default_branch=None)]})
        if request.method == "POST" and path.endswith("/pushes"):
            return httpx.Response(201, json={"pushId": 1})
        sub_response = _subresource_response(request, converged=True)
        if sub_response is not None:
            return sub_response
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client, fic_applied=True)
    context = _context(plan=valid_plan)

    outcome = await stage.execute(context)
    await http_client.aclose()

    assert outcome.status is StageStatus.SUCCEEDED
    assert outcome.idempotence_outcome is IdempotenceOutcome.APPLIED


async def test_raises_when_project_creation_operation_fails(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": []})
        if request.method == "POST" and path.endswith("/_apis/projects"):
            return httpx.Response(
                202,
                json={"id": "op-1", "status": "queued", "url": f"{ORG_URL}/_apis/operations/op-1"},
            )
        if request.method == "GET" and "/_apis/operations/op-1" in path:
            return httpx.Response(200, json={"id": "op-1", "status": "failed"})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client, poll_interval_seconds=0.0)
    context = _context(plan=valid_plan)

    with pytest.raises(DevOpsProjectStageError, match="failed"):
        await stage.execute(context)

    await http_client.aclose()


async def test_raises_when_project_has_no_matching_default_repository(valid_plan) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": [_project()]})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(200, json={"value": []})
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client)
    context = _context(plan=valid_plan)

    with pytest.raises(DevOpsProjectStageError, match="no default repository"):
        await stage.execute(context)

    await http_client.aclose()


async def test_neither_tenant_record_nor_constructor_org_url_raises_named_error(valid_plan) -> None:
    """No organization URL from either source -> a named error before any HTTP call."""
    import pytest

    from groundwork_orchestrator.stages.devops_project import DevOpsProjectStageError

    stage = _stage(organization_url=None)
    context = _context(plan=valid_plan)

    with pytest.raises(DevOpsProjectStageError, match="organization URL"):
        await stage.execute(context)


async def test_raises_when_subscription_has_no_bootstrap_identity(valid_plan) -> None:
    """FR-006a: a subscription queued for deployment without ever completing bootstrap-identity
    onboarding is a real, addressable gap — this stage must fail loudly, not skip the service
    connection or guess a client id."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/_apis/projects"):
            return httpx.Response(200, json={"value": [_project()]})
        if request.method == "GET" and path.endswith("/_apis/git/repositories"):
            return httpx.Response(
                200, json={"value": [_repository(default_branch="refs/heads/main")]}
            )
        sub_response = _subresource_response(request, converged=True)
        if sub_response is not None:
            return sub_response
        raise AssertionError(f"unexpected request: {request.method} {path}")

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    stage = _stage(http_client=http_client, bootstrapped=False)
    context = _context(plan=valid_plan)

    with pytest.raises(DevOpsProjectStageError, match="no bootstrap identity"):
        await stage.execute(context)

    await http_client.aclose()
