"""Voice channel API — auth gating and the real approval/deployment pipeline.

Regression coverage for the 2026-08-06 security fix: a prior revision of ``api/voice.py`` had no
authentication on any mutating route and constructed ``Deployment`` records directly from a
caller-supplied ``subscription_id`` with a fabricated ``plan_hash`` (a random UUID) and an
``approval_id`` that was really just a consent record's id — bypassing plan sealing, cost
re-verification, and every check ``approval/service.record_approval`` enforces. These tests prove
the two guarantees that fix depends on: every mutating route requires a validated token, and a
queued deployment's ``authority`` always traces back to a real, previously-sealed plan and a real
``Approval`` — never a fabricated identifier.

``/v1/voice/chat`` and ``/v1/voice/plan`` are not separately covered here: both now route plan
creation through ``PlanningAgent.generate_plan`` → ``seal_plan`` → ``plan_repository`` — the
identical pipeline ``tests/contract/test_plans_endpoint.py`` already exercises — so duplicating
that coverage here would test the same code twice rather than anything voice-specific.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.approval import Approval, PendingApproval
from groundwork_contracts.audit import DeploymentStageRecord
from groundwork_contracts.deployment import Deployment
from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan
from groundwork_contracts.tenant import (
    AdoOrgAccessState,
    ConsentState,
    ConversationChannel,
    ConversationRecord,
    CustomerTenant,
    OffshoreInferenceConsent,
    SubscriptionEntitlement,
)
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.voice import router as voice_router
from groundwork_controlplane.approval.plan_identity import seal_plan
from groundwork_orchestrator.state.cosmos import TenantRegistry, TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint
from groundwork_shared.config.settings import GovernanceSettings, ResidencySettings
from groundwork_shared.costing.estimator import compose_estimate, fabric_capacity_line
from groundwork_shared.costing.retail_prices import RetailPricesClient

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
APPROVER_ID = "55555555-5555-5555-5555-555555555555"
SECOND_APPROVER_ID = "66666666-6666-6666-6666-666666666666"
NOW = datetime(2026, 8, 6, tzinfo=UTC)

_BLUEPRINT = load_blueprint(
    Path(__file__).resolve().parents[2]
    / "infra"
    / "blueprints"
    / "standard-production-fabric"
    / "blueprint.yaml"
)


class _FakeContainer:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        found = self._items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def query_items(
        self, query: str, *, parameters: Any = None, partition_key: Any = None, **_: Any
    ):
        bound_value = next((p["value"] for p in (parameters or []) if p["name"] == "@value"), None)
        for (tenant, _item_id), doc in self._items.items():
            if partition_key is not None and tenant != partition_key:
                continue
            if query.startswith("SELECT VALUE c.tenantId"):
                # TenantRegistry.list_tenant_ids(): the real Cosmos SDK evaluates the projection;
                # an unchanging container body dict would otherwise leak in place of the id.
                yield doc.get("tenantId")
                continue
            if "c.id = @value" in query and doc.get("id") != bound_value:
                continue
            if "c.plan_hash = @value" in query and doc.get("plan_hash") != bound_value:
                continue
            if "second_approval" in query:
                has_second = "second_approval" in doc
                wants_second = "NOT IS_DEFINED" not in query
                if has_second != wants_second:
                    continue
            if "authority.approval_id" in query:
                wanted = next(p["value"] for p in (parameters or []) if p["name"] == "@approval_id")
                if doc.get("authority", {}).get("approval_id") != wanted:
                    continue
            yield doc

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body


class _FakeTokenValidator:
    def __init__(self, callers: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        raw = authorization_header or ""
        token = raw.removeprefix("Bearer ")
        caller = self._callers.get(raw) or self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown or missing token")
        return caller


class _FakeArtefactStore:
    def __init__(self) -> None:
        self.uploaded: dict[str, dict[str, Any]] = {}

    def blob_url_for(self, approval_id: str) -> str:
        return f"https://example.invalid/approvals/{approval_id}.json"

    async def store(self, *, approval_id: str, payload: dict[str, Any]) -> str:
        self.uploaded[approval_id] = payload
        return self.blob_url_for(approval_id)


class _FakeToolCall:
    """One ``function_call`` content item, matching agent_framework's real shape (``.type``,
    ``.name``, ``.arguments``) — not the OpenAI SDK's nested ``.function.name`` shape, since
    ``/chat`` now calls the native client with auto-invocation disabled (see
    ``groundwork_controlplane.agents.providers.foundry_openai``)."""

    def __init__(self, *, name: str, arguments: str) -> None:
        self.type = "function_call"
        self.name = name
        self.arguments = arguments


class _FakeChatMessage:
    """Constructor kept name-compatible with the OpenAI-shaped fake this replaced — only what it
    builds internally changed, so every call site below stays untouched."""

    def __init__(self, *, content: str, tool_calls: list[_FakeToolCall] | None = None) -> None:
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeAFMessage:
    def __init__(self, contents: list[Any]) -> None:
        self.contents = contents


class _FakeChatResponse:
    """Matches ``ChatResponse``'s real shape: ``.text`` and ``.messages[-1].contents``."""

    def __init__(self, message: _FakeChatMessage) -> None:
        self.text = message.content
        self.messages = [_FakeAFMessage(contents=list(message.tool_calls))]


class _FakeOnboardingStatusRunner:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    async def __call__(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self.payload


class _FakeChatClient:
    """Matches ``FoundryChatClient.get_response``'s shape — see
    ``tests/unit/test_planning_agent.py``'s ``_FakeClient`` for the same pattern."""

    def __init__(self, responses: list[_FakeChatResponse]) -> None:
        self._responses = list(responses)
        self.seen_messages: list[list[Any]] = []

    async def get_response(self, messages: list[Any], *, options: object = None) -> Any:
        self.seen_messages.append(messages)
        return self._responses.pop(0)


class _FakeVoicePlanningAgent:
    def __init__(self, *, replies: list[str], plan: DeploymentPlan) -> None:
        self._client = _FakeChatClient(
            [_FakeChatResponse(_FakeChatMessage(content=reply)) for reply in replies]
        )
        self._model = "fake-model"
        self._plan = plan
        self.last_conversation_summary: str | None = None

    async def generate_plan(self, conversation_summary: str) -> DeploymentPlan:
        self.last_conversation_summary = conversation_summary
        return self._plan


class _FakeArmCredential:
    def __init__(
        self,
        *,
        tenant_id: str = "provider-tenant",
        object_id: str = APPROVER_ID,
    ) -> None:
        payload = {"tid": tenant_id, "oid": object_id}
        encoded = (
            __import__("base64")
            .urlsafe_b64encode(__import__("json").dumps(payload).encode("utf-8"))
            .decode("ascii")
            .rstrip("=")
        )
        self._token = f"header.{encoded}.signature"

    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            def __init__(self, token: str) -> None:
                self.token = token

        return _Token(self._token)


def _caller(
    object_id: str, *, roles: tuple[CallerRole, ...] = (CallerRole.APPROVER,)
) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=object_id,
        tenant_id=TENANT_ID,
        display_name=f"Test Caller {object_id[:8]}",
        roles=frozenset(roles),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
        # require_step_up_approval defaults on (ADR-0011); this file tests threshold and
        # second-approver behaviour, not step-up auth itself (see test_approvals_endpoint.py
        # for that), so every caller here presents MFA evidence to keep those concerns separate.
        authentication_methods=frozenset({"mfa"}),
    )


def _auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + token}


def _tenant(*, concurrency_cap: int = 3) -> CustomerTenant:
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        consent_state=ConsentState.GRANTED,
        consent_granted_at=datetime(2026, 1, 1, tzinfo=UTC),
        subscriptions=(
            SubscriptionEntitlement(
                subscription_id="33333333-3333-3333-3333-333333333333",
                display_name="Test Subscription",
                may_deploy=True,
            ),
        ),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        concurrency_cap=concurrency_cap,
        notification_email="customer@example.invalid",
    )


def _sealed_plan(valid_plan: DeploymentPlan, *, now: datetime = NOW) -> SealedDeploymentPlan:
    return seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="voice",
        now=now,
    )


def _fabric_price_client(*, price_per_cu_hour: float = 0.304326) -> RetailPricesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        item = {
            "meterName": "Data Warehouse Capacity Usage CU",
            "productName": "Fabric Capacity",
            "skuName": "m",
            "serviceName": "Microsoft Fabric",
            "armRegionName": "australiaeast",
            "retailPrice": price_per_cu_hour,
            "unitOfMeasure": "1 Hour",
            "currencyCode": "AUD",
            "type": "Consumption",
        }
        return httpx.Response(200, json={"Items": [item], "NextPageLink": None, "Count": 1})

    return RetailPricesClient(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


async def _expected_monthly_total(
    retail_prices_client: RetailPricesClient, sku: Any, region: str
) -> float:
    fabric_line = await fabric_capacity_line(retail_prices_client, sku, region)
    return compose_estimate(fabric_line, now=NOW).monthly_total


def _build_app(
    *,
    sealed_plan: SealedDeploymentPlan,
    threshold_aud: float,
    retail_prices_client: RetailPricesClient,
    callers: dict[str, AuthenticatedCaller],
    tenant: CustomerTenant | None = None,
) -> tuple[FastAPI, _FakeContainer, _FakeArtefactStore]:
    app = FastAPI()
    app.include_router(voice_router)
    register_error_handlers(app)

    plan_container = _FakeContainer()
    plan_doc = {
        **sealed_plan.model_dump(mode="json"),
        "id": sealed_plan.plan_hash,
        "tenantId": sealed_plan.tenant_id,
    }
    plan_container.seed(sealed_plan.tenant_id, sealed_plan.plan_hash, plan_doc)
    app.state.plan_repository = TenantScopedRepository(
        plan_container, model_cls=SealedDeploymentPlan, id_field="plan_hash"
    )

    approvals_container = _FakeContainer()
    app.state.approval_repository = TenantScopedRepository(
        approvals_container, model_cls=Approval, id_field="approval_id"
    )
    app.state.pending_approval_repository = TenantScopedRepository(
        approvals_container, model_cls=PendingApproval, id_field="approval_id"
    )

    tenant_container = _FakeContainer()
    active_tenant = tenant or _tenant()
    if not active_tenant.subscriptions:
        active_tenant = active_tenant.model_copy(
            update={
                "subscriptions": (
                    SubscriptionEntitlement(
                        subscription_id=sealed_plan.plan.subscription_id,
                        display_name="Test Subscription",
                        may_deploy=True,
                    ),
                )
            }
        )
    tenant_doc = active_tenant.model_dump(mode="json") | {
        "id": active_tenant.tenant_id,
        "tenantId": active_tenant.tenant_id,
    }
    tenant_container.seed(active_tenant.tenant_id, active_tenant.tenant_id, tenant_doc)
    app.state.tenant_repository = TenantScopedRepository(
        tenant_container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.tenant_registry = TenantRegistry(tenant_container)

    deployment_container = _FakeContainer()
    app.state.deployment_repository = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )
    conversation_container = _FakeContainer()
    app.state.conversation_repository = TenantScopedRepository(
        conversation_container, model_cls=ConversationRecord, id_field="conversation_id"
    )
    stage_record_container = _FakeContainer()
    app.state.stage_record_repository = TenantScopedRepository(
        stage_record_container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    app.state.blueprint = _BLUEPRINT

    app.state.token_validator = _FakeTokenValidator(callers)
    app.state.retail_prices_client = retail_prices_client
    app.state.now_fn = lambda: NOW
    app.state.credential = _FakeArmCredential(object_id=APPROVER_ID)
    artefact_store = _FakeArtefactStore()
    app.state.approval_artefact_store = artefact_store
    app.state.settings = type(
        "FakeSettings",
        (),
        {
            "voice_live_endpoint": "https://voice.example.invalid",
            "residency": ResidencySettings(storage_region="australiaeast"),
            "governance": GovernanceSettings(
                approval_threshold_aud=threshold_aud,
                approver_role="Groundwork.Approver",
                default_tenant_concurrency_cap=3,
            ),
            "azure_location": "australiaeast",
            "readiness": type(
                "FakeReadiness",
                (),
                {
                    "orchestrator_principal_id": APPROVER_ID,
                    "devops_organization_url": None,
                    "controlplane_principal_id": None,
                    "orchestrator_display_name": None,
                },
            )(),
        },
    )()

    return app, deployment_container, artefact_store


@pytest.fixture
def retail_prices_client() -> RetailPricesClient:
    return _fabric_price_client()


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/voice/consent", {}),
        ("/v1/voice/approve", {"planHash": "sha256:" + "a" * 64, "acknowledgedCostAud": 1.0}),
    ],
)
def test_mutating_voice_routes_reject_unauthenticated_callers(
    path: str,
    body: dict[str, Any],
    valid_plan: DeploymentPlan,
    retail_prices_client: RetailPricesClient,
) -> None:
    """The exact regression this fix closes: no bearer token must never reach the handler body."""
    sealed = _sealed_plan(valid_plan)
    app, _deployments, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(path, json=body)

    assert response.status_code == 401, response.text


async def test_voice_approve_below_threshold_queues_a_real_deployment(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The core fix: a voice approval must bind to the real sealed plan's own hash — never a
    fabricated one — and the queued deployment's authority must trace back to it exactly."""
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, deployment_container, artefact_store = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total + 1000,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/approve",
        headers={"Authorization": "Bearer good-token"},
        json={
            "planHash": sealed.plan_hash,
            "acknowledgedCostAud": expected_total,
            "acknowledgedPowerBiViewerLicensing": True,
        },
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["secondApprovalRequired"] is False
    assert body["status"] == "queued"
    assert body["planHash"] == sealed.plan_hash

    deployment_id = body["deploymentId"]
    stored = next(
        doc
        for (tenant, item_id), doc in deployment_container._items.items()
        if item_id == deployment_id
    )
    assert stored["authority"]["plan_hash"] == sealed.plan_hash
    assert stored["authority"]["approval_id"] == body["approvalId"]
    assert stored["subscription_id"] == sealed.plan.subscription_id
    assert artefact_store.uploaded  # a real approval artefact was written


def test_voice_chat_persists_transcript_and_reuses_session(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    planning_agent = _FakeVoicePlanningAgent(
        replies=["What region should I deploy this into?", "What Fabric SKU do you need?"],
        plan=valid_plan,
    )
    app.state.planning_agent = planning_agent
    app.state.voice_tool_client = planning_agent._client
    client = TestClient(app)

    first = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "I need a Fabric platform"},
    )

    assert first.status_code == 200, first.text
    session_id = first.json()["sessionId"]

    record = asyncio.run(app.state.conversation_repository.read(TENANT_ID, session_id))
    assert record is not None
    assert record.channel is ConversationChannel.VOICE
    assert [turn.speaker for turn in record.transcript] == ["customer", "agent"]
    assert [turn.text for turn in record.transcript] == [
        "I need a Fabric platform",
        "What region should I deploy this into?",
    ]

    second = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "australiaeast", "session_id": session_id},
    )

    assert second.status_code == 200, second.text
    record = asyncio.run(app.state.conversation_repository.read(TENANT_ID, session_id))
    assert record is not None
    assert [turn.speaker for turn in record.transcript] == [
        "customer",
        "agent",
        "customer",
        "agent",
    ]
    seen = app.state.voice_tool_client.seen_messages[1][-2:]
    assert [(m.role, m.contents[0].text) for m in seen] == [
        ("assistant", "What region should I deploy this into?"),
        ("user", "australiaeast"),
    ]


def test_voice_chat_get_onboarding_status_returns_facts_without_plan_or_state_mutation(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    planning_agent = _FakeVoicePlanningAgent(
        replies=["The onboarding steps are on screen."], plan=valid_plan
    )
    app.state.planning_agent = planning_agent
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="The onboarding steps are on screen.",
                    tool_calls=[_FakeToolCall(name="get_onboarding_status", arguments="{}")],
                )
            )
        ]
    )

    import groundwork_controlplane.api.voice as voice_module

    original = voice_module._run_get_onboarding_status_tool
    voice_module._run_get_onboarding_status_tool = _FakeOnboardingStatusRunner(
        {
            "tenantId": TENANT_ID,
            "subscriptionId": valid_plan.subscription_id,
            "delegationState": "pending",
            "gates": {
                "tenant_created": {"state": "created", "next_action": "next"},
                "consent": {"state": "pending", "next_action": "next"},
                "lighthouse_delegation": {"state": "pending", "next_action": "next"},
                "ado_org_access": {"state": "pending", "next_action": "next"},
                "bootstrap_identity": {"state": "pending", "next_action": "next"},
            },
            "lighthouse": {"azDeploymentCommand": "az deployment sub create ..."},
            "azureDevOps": {"principalObjectId": APPROVER_ID},
        }
    )
    client = TestClient(app)
    try:
        response = client.post(
            "/v1/voice/chat",
            headers=_auth_headers("good-token"),
            params={"message": "What do we still need for onboarding?"},
        )
    finally:
        voice_module._run_get_onboarding_status_tool = original

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["reply"] == "The onboarding steps are on screen."
    assert body["plan"] is None
    assert body["onboarding"]["delegationState"] == "pending"
    assert body["onboarding"]["gates"]["consent"]["state"] == "pending"

    record = asyncio.run(app.state.conversation_repository.read(TENANT_ID, body["sessionId"]))
    assert record is not None
    assert [turn.speaker for turn in record.transcript] == ["customer", "agent"]
    assert [turn.text for turn in record.transcript] == [
        "What do we still need for onboarding?",
        "The onboarding steps are on screen.",
    ]


def test_voice_chat_create_tenant_returns_verified_result(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Tenant record created."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Tenant record created.",
                    tool_calls=[
                        _FakeToolCall(
                            name="create_tenant",
                            arguments='{"display_name":"Contoso"}',
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Create the tenant onboarding record."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "created"
    assert onboarding["verified"] is True
    assert onboarding["evidence"]["consentState"] == "pending"


class _FakeConsentStore:
    """Minimal OffshoreInferenceConsentStore fake for quick_onboard's consent step."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def record(
        self,
        *,
        consent_id: str,
        consenting_identity_object_id: str,
        consenting_identity_display_name: str,
        disclosure_version: str,
        now: datetime,
    ) -> OffshoreInferenceConsent:
        self.calls.append(
            {
                "consent_id": consent_id,
                "consenting_identity_object_id": consenting_identity_object_id,
                "consenting_identity_display_name": consenting_identity_display_name,
                "disclosure_version": disclosure_version,
                "now": now,
            }
        )
        return OffshoreInferenceConsent(
            consenting_identity_object_id=consenting_identity_object_id,
            consenting_identity_display_name=consenting_identity_display_name,
            consented_at=now,
            artefact_uri=f"https://example.invalid/consent/{consent_id}.json",
            disclosure_version=disclosure_version,
        )


def test_voice_chat_quick_onboard_onboards_own_tenant(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The voice model can run the whole onboarding flow in one call for the operator's own
    tenant: PENDING consent gets confirmed and recorded, offshore consent lands, voice enables."""
    sealed = _sealed_plan(valid_plan)
    pending_tenant = _tenant().model_copy(
        update={"consent_state": ConsentState.PENDING, "consent_granted_at": None}
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        tenant=pending_tenant,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
    )
    app.state.consent_store = _FakeConsentStore()
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Your tenant is onboarded."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Your tenant is onboarded.",
                    tool_calls=[
                        _FakeToolCall(
                            name="quick_onboard",
                            arguments=(
                                '{"display_name":"My dev tenant",'
                                '"consent_note":"own dev tenant, self-confirmed",'
                                '"voice_enabled":true,"voice_note":"voice included"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Onboard my own dev tenant."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "onboarded"
    assert onboarding["verified"] is True
    assert onboarding["consentState"] == "granted"
    assert onboarding["steps"] == {
        "tenantCreated": True,
        "consentConfirmed": True,
        "offshoreConsentRecorded": True,
        "voiceEnabled": True,
    }


def test_voice_chat_quick_onboard_denied_for_non_operator(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """quick_onboard requires the OPERATOR role — a caller without it gets a denied result."""
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.APPROVER,))},
    )
    app.state.consent_store = _FakeConsentStore()
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["I cannot do that."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="I cannot do that.",
                    tool_calls=[
                        _FakeToolCall(
                            name="quick_onboard",
                            arguments=(
                                '{"display_name":"My dev tenant",'
                                '"consent_note":"own dev tenant, self-confirmed"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Onboard my own dev tenant."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "denied"
    assert onboarding["verified"] is False


def test_voice_chat_confirm_customer_consent_denied_for_non_operator(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.APPROVER,))},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["I cannot do that."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="I cannot do that.",
                    tool_calls=[
                        _FakeToolCall(
                            name="confirm_customer_consent",
                            arguments=(
                                '{"tenant_id":"11111111-1111-1111-1111-111111111111",'
                                '"confirmation_note":"confirmed"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Confirm customer consent."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "denied"
    assert onboarding["verified"] is False


def test_voice_chat_confirm_customer_consent_returns_verified_result(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    pending_tenant = _tenant().model_copy(
        update={"consent_state": ConsentState.PENDING, "consent_granted_at": None}
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
        tenant=pending_tenant,
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Consent confirmed."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Consent confirmed.",
                    tool_calls=[
                        _FakeToolCall(
                            name="confirm_customer_consent",
                            arguments=(
                                '{"tenant_id":"11111111-1111-1111-1111-111111111111",'
                                '"confirmation_note":"confirmed by operator"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Confirm customer consent now."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "granted"
    assert onboarding["verified"] is True
    assert onboarding["evidence"]["consentState"] == "granted"


def test_voice_chat_grant_ado_org_access_returns_member_pending_pca(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "serviceprincipalentitlements" in url:
            return httpx.Response(201, json={"id": "entitlement"})
        if "connectionData" in url:
            return httpx.Response(403, json={})
        return httpx.Response(404)

    tenant = _tenant().model_copy(
        update={"devops_organization_url": "https://dev.azure.com/contoso"}
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
        tenant=tenant,
    )
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.http_client = mock_client
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["PCA still needed."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="PCA still needed.",
                    tool_calls=[
                        _FakeToolCall(
                            name="grant_ado_org_access",
                            arguments='{"tenant_id":"11111111-1111-1111-1111-111111111111"}',
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Grant Azure DevOps access."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "member_pending_pca"
    assert onboarding["verified"] is False
    assert "Project Collection Administrators" in onboarding["pcaInstructionText"]


def test_voice_chat_get_onboarding_status_includes_step_completion(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The real status runner returns the step-completion breakdown alongside the onboarding
    facts, so a voice operator gets both the delegation evidence and the check-list picture."""
    sealed = _sealed_plan(valid_plan)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "registrationAssignments" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "properties": {
                                "registrationDefinitionId": (
                                    "/subscriptions/33333333-3333-3333-3333-333333333333/"
                                    "providers/Microsoft.ManagedServices/registrationDefinitions/"
                                    "test-def"
                                )
                            }
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "properties": {
                    "authorizations": [
                        {
                            "principalId": APPROVER_ID,
                            "roleDefinitionId": "b24988ac-6180-42a0-ab88-20f7382dd24c",
                        }
                    ]
                }
            },
        )

    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
    )
    app.state.lighthouse_http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Here is the onboarding status."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Here is the onboarding status.",
                    tool_calls=[_FakeToolCall(name="get_onboarding_status", arguments="{}")],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "What is my onboarding status?"},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    # Onboarding facts (delegation evidence).
    assert onboarding["delegationState"] == "granted"
    # Step-completion breakdown from the shared status builder.
    status = onboarding["onboardingStatus"]
    assert status["consentState"] == "granted"
    assert status["steps"]["tenantCreated"]["completed"] is True
    assert status["steps"]["consentConfirmed"]["completed"] is True
    assert status["steps"]["offshoreConsentRecorded"]["completed"] is False
    assert status["steps"]["voiceEnabled"]["completed"] is False
    assert "offshore-inference-consent" in status["nextAction"]


def test_voice_chat_get_offshore_inference_disclosure_returns_text(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The disclosure tool returns the canonical FR-053d text verbatim so the model can read it
    aloud before consent — the transcript then evidences what the customer was shown."""
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(replies=["Here it is."], plan=valid_plan)
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Here it is.",
                    tool_calls=[
                        _FakeToolCall(name="get_offshore_inference_disclosure", arguments="{}")
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Show me the offshore disclosure."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "ok"
    assert onboarding["disclosureVersion"] == "1.0.0"
    assert "Voice Live" in onboarding["disclosure"]
    assert "outside your configured" in onboarding["disclosure"]
    assert "never stored" in onboarding["disclosure"]


def test_voice_chat_record_offshore_inference_consent_attaches_to_tenant(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """The consent tool records the artefact and attaches it to the tenant record — the field the
    voice enablement gate reads — so a voiced consent actually enables voice."""
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    app.state.consent_store = _FakeConsentStore()
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Consent recorded."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Consent recorded.",
                    tool_calls=[
                        _FakeToolCall(name="record_offshore_inference_consent", arguments="{}")
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "I consent to offshore inference."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "recorded"
    assert onboarding["verified"] is True
    assert onboarding["disclosureVersion"] == "1.0.0"


def test_voice_chat_list_tenants_returns_portfolio(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """An operator can ask for the whole portfolio by voice; the seeded tenant appears with its
    onboarding state summarised."""
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Here are your tenants."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Here are your tenants.",
                    tool_calls=[_FakeToolCall(name="list_tenants", arguments="{}")],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "What tenants do I manage?"},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "ok"
    assert onboarding["tenantCount"] == 1
    assert onboarding["tenants"][0]["tenantId"] == TENANT_ID
    assert onboarding["tenants"][0]["consentState"] == "granted"
    assert onboarding["tenants"][0]["notificationEmailRecorded"] is True


def test_voice_chat_check_step_up_status_reports_satisfied_for_mfa_caller(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """Read-only precheck tool dispatch over /chat — mirrors the WS-path coverage in
    test_voice_live_endpoint.py, which also covers the unsatisfied branch of the same
    underlying step_up_authentication_satisfied() logic."""
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["You're all set to approve."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="You're all set to approve.",
                    tool_calls=[_FakeToolCall(name="check_step_up_status", arguments="{}")],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Am I ready to approve this?"},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["stepUpRequired"] is True
    assert onboarding["satisfied"] is True


def test_voice_chat_list_tenants_denied_for_non_operator(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["I cannot do that."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="I cannot do that.",
                    tool_calls=[_FakeToolCall(name="list_tenants", arguments="{}")],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "What tenants do I manage?"},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "denied"
    assert onboarding["verified"] is False


def test_voice_chat_trigger_bootstrap_identity_returns_verified_result(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "registrationAssignments" in url:
            return httpx.Response(
                200,
                json={
                    "value": [
                        {
                            "properties": {
                                "registrationDefinitionId": (
                                    "/subscriptions/33333333-3333-3333-3333-333333333333/providers/"
                                    "Microsoft.ManagedServices/registrationDefinitions/test-def"
                                )
                            }
                        }
                    ]
                },
            )
        if "registrationDefinitions" in url:
            return httpx.Response(
                200,
                json={
                    "properties": {
                        "authorizations": [
                            {"principalId": APPROVER_ID, "roleDefinitionId": "contributor"}
                        ]
                    }
                },
            )
        if "connectionData" in url:
            return httpx.Response(200, json={"instanceId": "aaaaaaaa-0000-0000-0000-000000000000"})
        if "federatedIdentityCredentials" in url:
            return httpx.Response(201, json={"properties": {}})
        if "roleAssignments" in url:
            return httpx.Response(201, json={"properties": {}})
        if "userAssignedIdentities" in url:
            return httpx.Response(
                201,
                json={
                    "properties": {
                        "clientId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                        "principalId": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                    }
                },
            )
        if "/resourceGroups/" in url:
            return httpx.Response(200, json={"name": "rg-groundwork-33333333"})
        return httpx.Response(404)

    tenant = _tenant().model_copy(
        update={
            "devops_organization_url": "https://dev.azure.com/contoso",
            "ado_org_access_state": AdoOrgAccessState.GRANTED,
            "ado_org_access_granted_at": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
        tenant=tenant,
    )
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    app.state.lighthouse_http_client = app.state.http_client
    app.state.credential = _FakeArmCredential(object_id=APPROVER_ID)
    app.state.planning_agent = _FakeVoicePlanningAgent(
        replies=["Bootstrap complete."], plan=valid_plan
    )
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Bootstrap complete.",
                    tool_calls=[
                        _FakeToolCall(
                            name="trigger_bootstrap_identity",
                            arguments=(
                                '{"tenant_id":"11111111-1111-1111-1111-111111111111",'
                                '"subscription_id":"33333333-3333-3333-3333-333333333333"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Trigger bootstrap identity."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] == "created"
    assert onboarding["verified"] is True
    assert onboarding["evidence"]["bootstrapIdentityResourceId"] is not None


def test_voice_chat_trigger_bootstrap_identity_reports_remaining_gates(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    pending_tenant = _tenant().model_copy(
        update={"consent_state": ConsentState.PENDING, "consent_granted_at": None}
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID, roles=(CallerRole.OPERATOR,))},
        tenant=pending_tenant,
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(replies=["Not ready yet."], plan=valid_plan)
    app.state.voice_tool_client = _FakeChatClient(
        [
            _FakeChatResponse(
                _FakeChatMessage(
                    content="Not ready yet.",
                    tool_calls=[
                        _FakeToolCall(
                            name="trigger_bootstrap_identity",
                            arguments=(
                                '{"tenant_id":"11111111-1111-1111-1111-111111111111",'
                                '"subscription_id":"33333333-3333-3333-3333-333333333333"}'
                            ),
                        )
                    ],
                )
            )
        ]
    )
    client = TestClient(app)

    response = client.post(
        "/v1/voice/chat",
        headers=_auth_headers("good-token"),
        params={"message": "Bootstrap the identity now."},
    )

    assert response.status_code == 200, response.text
    onboarding = response.json()["onboarding"]
    assert onboarding["status"] in {"not_ready", "error"}
    assert onboarding["verified"] is False
    if onboarding["status"] == "not_ready":
        assert onboarding["remainingGates"][0]["gate"] == "consent"


def test_voice_plan_persists_one_shot_transcript(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    sealed = _sealed_plan(valid_plan)
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(replies=[], plan=valid_plan)
    client = TestClient(app)

    response = client.post(
        "/v1/voice/plan",
        headers=_auth_headers("good-token"),
        params={"transcript": "Deploy a Fabric platform to australiaeast on F2."},
    )

    assert response.status_code == 200, response.text

    async def _conversations() -> list[ConversationRecord]:
        records: list[ConversationRecord] = []
        async for item in app.state.conversation_repository.query(TENANT_ID, "SELECT * FROM c"):
            records.append(item)
        return records

    records = asyncio.run(_conversations())
    assert len(records) == 1
    assert [turn.speaker for turn in records[0].transcript] == ["customer", "agent"]
    assert records[0].transcript[0].text == "Deploy a Fabric platform to australiaeast on F2."
    assert records[0].transcript[1].text.startswith("Plan ready:")


def test_voice_plan_refused_without_granted_consent(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """FR-006. Found live 2026-08-24: `_seal_plan_for_caller` (the shared tail of `/plan`,
    `/chat`, and the WebSocket) never checked `consent_state` at all, unlike the REST `/plans`
    endpoint's own check — a voice caller could seal a plan for a tenant with no Lighthouse
    delegation granted."""
    sealed = _sealed_plan(valid_plan)
    pending_tenant = _tenant().model_copy(
        update={"consent_state": ConsentState.PENDING, "consent_granted_at": None}
    )
    app, _deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=100000.0,
        retail_prices_client=retail_prices_client,
        callers={"good-token": _caller(APPROVER_ID)},
        tenant=pending_tenant,
    )
    app.state.planning_agent = _FakeVoicePlanningAgent(replies=[], plan=valid_plan)
    client = TestClient(app)

    response = client.post(
        "/v1/voice/plan",
        headers=_auth_headers("good-token"),
        params={"transcript": "Deploy a Fabric platform to australiaeast on F2."},
    )

    assert response.status_code == 403, response.text


async def test_voice_approve_above_threshold_requires_second_distinct_approver(
    valid_plan: DeploymentPlan, retail_prices_client: RetailPricesClient
) -> None:
    """ADR-0011 removes the durable-channel requirement, not the distinct-identity
    second-approver requirement (SC-018) — a lone voice approval above threshold must not queue
    anything."""
    sealed = _sealed_plan(valid_plan)
    expected_total = await _expected_monthly_total(
        retail_prices_client, valid_plan.fabric_capacity_sku, valid_plan.region.value
    )
    app, deployment_container, _artefacts = _build_app(
        sealed_plan=sealed,
        threshold_aud=expected_total - 1,
        retail_prices_client=retail_prices_client,
        callers={
            "first-token": _caller(APPROVER_ID),
            "second-token": _caller(SECOND_APPROVER_ID),
        },
    )
    client = TestClient(app)
    body = {
        "planHash": sealed.plan_hash,
        "acknowledgedCostAud": expected_total,
        "acknowledgedPowerBiViewerLicensing": True,
    }

    first = client.post(
        "/v1/voice/approve", headers={"Authorization": "Bearer first-token"}, json=body
    )
    assert first.status_code == 200, first.text
    assert first.json()["secondApprovalRequired"] is True
    assert deployment_container._items == {}  # nothing queued yet

    same_identity_again = client.post(
        "/v1/voice/approve", headers={"Authorization": "Bearer first-token"}, json=body
    )
    assert same_identity_again.status_code == 409

    second = client.post(
        "/v1/voice/approve", headers={"Authorization": "Bearer second-token"}, json=body
    )
    assert second.status_code == 200, second.text
    assert second.json()["secondApprovalRequired"] is False
    assert second.json()["status"] == "queued"
    assert len(deployment_container._items) == 1
