"""Tenant onboarding (FR-006) — create, consent-url generation, operator-attestation confirm.

Covers the design decided 2026-08-06 (see ``api/tenants.py``'s module docstring): every tenant
starts ``PENDING``, the consent-url route never mutates anything, and only an authenticated
``CallerRole.OPERATOR`` confirming out-of-band can flip a tenant to ``GRANTED`` — recorded on the
tenant record itself so the attestation is durable and auditable.
"""

from __future__ import annotations

import asyncio
import base64
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_channels.voice.consent import CURRENT_DISCLOSURE_VERSION
from groundwork_contracts.tenant import ConsentState, CustomerTenant, OffshoreInferenceConsent
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.lighthouse_onboarding import router as lighthouse_onboarding_router
from groundwork_controlplane.api.tenants import router as tenants_router
from groundwork_orchestrator.state.cosmos import TenantScopedRepository, from_document
from groundwork_shared.config.settings import EntraSettings, GovernanceSettings

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OPERATOR_ID = "77777777-7777-7777-7777-777777777777"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"
NOW = datetime(2026, 8, 6, tzinfo=UTC)


class _FakeContainer:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceExistsError

        key = (body["tenantId"], body["id"])
        if key in self._items:
            raise CosmosResourceExistsError(status_code=409, message="already exists")
        self._items[key] = body
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
        for (tenant, _item_id), doc in self._items.items():
            if partition_key is not None and tenant != partition_key:
                continue
            yield doc


class _FakeTokenValidator:
    def __init__(self, callers: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        token = (authorization_header or "").removeprefix("Bearer ")
        caller = self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown or missing token")
        return caller


class _FakeConsentStore:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

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


def _caller(object_id: str, *, roles: tuple[CallerRole, ...]) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=object_id,
        tenant_id=TENANT_ID,
        display_name=f"Test Caller {object_id[:8]}",
        roles=frozenset(roles),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


class _FakeCredential:
    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            token = "fake-token"  # noqa: S105

        return _Token()


class _FakeArmCredential:
    def __init__(
        self,
        *,
        tenant_id: str = "provider-tenant",
        object_id: str = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
    ) -> None:
        payload = {"tid": tenant_id, "oid": object_id}
        encoded = _b64url(json.dumps(payload).encode("utf-8"))
        self._token = f"header.{encoded}.signature"

    async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
        class _Token:
            def __init__(self, token: str) -> None:
                self.token = token

        return _Token(self._token)


def _b64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _build_app(
    *,
    callers: dict[str, AuthenticatedCaller],
    redirect_uri: str | None = "https://portal.azure.com",
    http_handler: Any = None,
    credential: Any = None,
    controlplane_principal_id: str | None = None,
) -> tuple[FastAPI, _FakeContainer, _FakeConsentStore]:
    app = FastAPI()
    app.include_router(tenants_router)
    app.include_router(lighthouse_onboarding_router)
    register_error_handlers(app)

    container = _FakeContainer()
    app.state.tenant_repository = TenantScopedRepository(
        container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.token_validator = _FakeTokenValidator(callers)
    app.state.now_fn = lambda: NOW
    app.state.credential = credential or _FakeCredential()
    consent_store = _FakeConsentStore()
    app.state.consent_store = consent_store
    if http_handler is not None:
        mock_client = httpx.AsyncClient(transport=httpx.MockTransport(http_handler))
        app.state.http_client = mock_client
        app.state.lighthouse_http_client = mock_client
    app.state.settings = type(
        "FakeSettings",
        (),
        {
            "azure_location": "australiaeast",
            "entra": EntraSettings(
                tenant_id="provider-tenant",
                client_id="00000000-0000-0000-0000-000000000000",
                audience="api://00000000-0000-0000-0000-000000000000",
                redirect_uri=redirect_uri,
            ),
            "readiness": type(
                "FakeReadiness",
                (),
                {
                    "orchestrator_principal_id": OPERATOR_ID,
                    "devops_organization_url": None,
                    "controlplane_principal_id": controlplane_principal_id,
                    "orchestrator_display_name": None,
                },
            )(),
            "governance": GovernanceSettings(
                approval_threshold_aud=1000.0,
                approver_role="Groundwork.Approver",
                default_tenant_concurrency_cap=3,
            ),
        },
    )()

    return app, container, consent_store


def _create_body() -> dict[str, Any]:
    return {
        "tenantId": TENANT_ID,
        "displayName": "Test Customer",
        "approvedRegions": ["australiaeast"],
        "dataResidencyRegions": ["australiaeast"],
    }


def test_create_tenant_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body()
    )

    assert response.status_code == 403, response.text


def test_create_tenant_rejects_unauthenticated_callers() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post("/v1/tenants", json=_create_body())

    assert response.status_code == 401, response.text


def test_create_tenant_starts_pending_and_rejects_duplicates() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    first = client.post(
        "/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body()
    )
    assert first.status_code == 201, first.text
    assert first.json()["consentState"] == "pending"
    assert first.json()["consentGrantedAt"] is None

    duplicate = client.post(
        "/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body()
    )
    assert duplicate.status_code == 409


def test_consent_url_is_pure_and_reflects_real_client_id() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/consent-url",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    url = response.json()["consentUrl"]
    assert url.startswith(f"https://login.microsoftonline.com/{TENANT_ID}/v2.0/adminconsent?")
    assert "client_id=00000000-0000-0000-0000-000000000000" in url
    assert "redirect_uri=https%3A%2F%2Fportal.azure.com" in url

    # Pure — no state mutation. The tenant is still pending after generating the URL.
    status = client.get(f"/v1/tenants/{TENANT_ID}", headers={"Authorization": "Bearer good-token"})
    assert status.json()["consentState"] == "pending"


def test_consent_url_is_503_when_redirect_uri_not_configured() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        redirect_uri=None,
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/consent-url",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 503


def test_operator_attestation_grants_consent_and_records_who() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "confirmed by customer admin directly over email"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consentState"] == "granted"
    assert body["consentGrantedAt"] == NOW.isoformat()
    assert body["consentConfirmedBy"]["objectId"] == OPERATOR_ID
    assert body["consentConfirmedBy"]["note"] == "confirmed by customer admin directly over email"


def test_confirming_an_already_granted_tenant_is_409() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())
    client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "first confirmation"},
    )

    second = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "second confirmation attempt"},
    )

    assert second.status_code == 409


def test_confirming_an_unknown_tenant_is_404() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "n/a"},
    )

    assert response.status_code == 404


def test_operator_attestation_grants_ado_org_access_independent_of_consent() -> None:
    """FR-038b. Confirming ADO org access must not require consent to already be granted, and
    must not itself flip consent_state — the two fields are independent."""
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "confirmed System's identity was added to the customer's ADO org"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["adoOrgAccessState"] == "granted"
    assert body["adoOrgAccessGrantedAt"] == NOW.isoformat()
    assert body["adoOrgAccessConfirmedBy"]["objectId"] == OPERATOR_ID
    assert (
        body["adoOrgAccessConfirmedBy"]["note"]
        == "confirmed System's identity was added to the customer's ADO org"
    )
    assert body["consentState"] == "pending"  # unaffected


def test_confirming_an_already_granted_ado_org_access_is_409() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())
    client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "first confirmation"},
    )

    second = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "second confirmation attempt"},
    )

    assert second.status_code == 409


def test_confirming_ado_org_access_for_an_unknown_tenant_is_404() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/onboarding/ado-access-confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "n/a"},
    )

    assert response.status_code == 404


def test_confirming_ado_org_access_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=())}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "n/a"},
    )

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Bootstrap identity (FR-006a)
# ---------------------------------------------------------------------------

SUBSCRIPTION_ID = "33333333-3333-3333-3333-333333333333"
ORG_URL = "https://dev.azure.com/contoso"
ORGANIZATION_GUID = "aaaaaaaa-0000-0000-0000-000000000000"


def _bootstrap_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if "connectionData" in url:
        return httpx.Response(200, json={"instanceId": ORGANIZATION_GUID})
    if "federatedIdentityCredentials" in url:
        return httpx.Response(201, json={"properties": {}})
    if "roleAssignments" in url:
        return httpx.Response(201, json={"properties": {}})
    if "userAssignedIdentities" in url:
        return httpx.Response(
            201,
            json={
                "id": (
                    f"/subscriptions/{SUBSCRIPTION_ID}/resourceGroups/"
                    f"rg-groundwork-{SUBSCRIPTION_ID[:8]}/providers/Microsoft.ManagedIdentity/"
                    "userAssignedIdentities/uami-gw-test"
                ),
                "properties": {
                    "clientId": "cccccccc-cccc-cccc-cccc-cccccccccccc",
                    "principalId": "dddddddd-dddd-dddd-dddd-dddddddddddd",
                },
            },
        )
    if "/resourceGroups/" in url:
        return httpx.Response(200, json={"name": "rg-groundwork-33333333"})
    return httpx.Response(404)


async def _granted_tenant_with_subscription(app: FastAPI, client: TestClient) -> None:
    """Create a tenant, confirm consent, add a subscription entitlement, and record a
    devops_organization_url — the three preconditions ``bootstrap_identity`` checks."""
    client.post(
        "/v1/tenants",
        headers={"Authorization": "Bearer good-token"},
        json={
            **_create_body(),
            "subscriptions": [
                {
                    "subscription_id": SUBSCRIPTION_ID,
                    "display_name": "Test Subscription",
                    "may_deploy": True,
                }
            ],
        },
    )
    client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "confirmed"},
    )
    repo = app.state.tenant_repository
    tenant = await repo.read(TENANT_ID, TENANT_ID)
    updated = tenant.model_copy(update={"devops_organization_url": ORG_URL})
    await repo.replace(TENANT_ID, updated)


def test_bootstrap_identity_creates_uami_and_federated_credential() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_bootstrap_handler,
    )
    client = TestClient(app)

    asyncio.run(_granted_tenant_with_subscription(app, client))

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    sub = next(s for s in body["subscriptions"] if s["subscriptionId"] == SUBSCRIPTION_ID)
    assert sub["bootstrapIdentityClientId"] == "cccccccc-cccc-cccc-cccc-cccccccccccc"


def test_bootstrap_identity_is_idempotent_on_second_call() -> None:
    calls = {"count": 0}

    def counting_handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _bootstrap_handler(request)

    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=counting_handler,
    )
    client = TestClient(app)

    asyncio.run(_granted_tenant_with_subscription(app, client))

    first = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )
    calls_after_first = calls["count"]
    second = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert calls["count"] == calls_after_first  # no new Azure calls on the second attempt


def test_bootstrap_identity_refused_without_granted_consent() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_bootstrap_handler,
    )
    client = TestClient(app)
    client.post(
        "/v1/tenants",
        headers={"Authorization": "Bearer good-token"},
        json={
            **_create_body(),
            "subscriptions": [
                {
                    "subscription_id": SUBSCRIPTION_ID,
                    "display_name": "Test Subscription",
                    "may_deploy": True,
                }
            ],
        },
    )

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 409


def test_bootstrap_identity_refused_for_unentitled_subscription() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_bootstrap_handler,
    )
    client = TestClient(app)
    client.post("/v1/tenants", headers={"Authorization": "Bearer good-token"}, json=_create_body())
    client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers={"Authorization": "Bearer good-token"},
        json={"note": "confirmed"},
    )

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 404


def test_bootstrap_identity_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=())}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions/{SUBSCRIPTION_ID}/bootstrap-identity",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403


def _create_tenant_with_subscription(client: TestClient) -> None:
    created = client.post(
        "/v1/tenants",
        headers={"Authorization": "Bearer good-token"},
        json={
            **_create_body(),
            "subscriptions": [
                {
                    "subscription_id": SUBSCRIPTION_ID,
                    "display_name": "Test Subscription",
                    "may_deploy": True,
                }
            ],
        },
    )
    assert created.status_code == 201, created.text


def _lighthouse_handler_factory(*, granted: bool) -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "registrationAssignments" in url:
            if not granted:
                return httpx.Response(200, json={"value": []})
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
        return httpx.Response(
            200,
            json={
                "properties": {
                    "authorizations": [
                        {
                            "principalId": "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                            "roleDefinitionId": "b24988ac-6180-42a0-ab88-20f7382dd24c",
                        }
                    ]
                }
            },
        )

    return handler


def test_lighthouse_onboarding_requires_authentication() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.get(f"/v1/tenants/{TENANT_ID}/onboarding/lighthouse")

    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_lighthouse_onboarding_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/lighthouse",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403
    assert response.headers["content-type"] == "application/problem+json"


def test_lighthouse_onboarding_reports_pending_state() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_lighthouse_handler_factory(granted=False),
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/lighthouse",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delegationState"] == "pending"
    assert body["subscriptionId"] == SUBSCRIPTION_ID
    assert body["gates"]["tenant_created"]["state"] == "created"
    assert body["gates"]["consent"]["state"] == "pending"
    assert body["lighthouse"]["azDeploymentCommand"].startswith(
        f"az deployment sub create --subscription {SUBSCRIPTION_ID}"
    )
    assert body["azureDevOps"]["principalObjectId"] == OPERATOR_ID


def test_lighthouse_onboarding_reports_granted_state() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_lighthouse_handler_factory(granted=True),
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/lighthouse",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delegationState"] == "granted"
    assert body["gates"]["lighthouse_delegation"]["state"] == "granted"
    assert (
        body["azureDevOps"]["servicePrincipalEntitlementsEndpoint"]
        == "https://vsaex.dev.azure.com/<your-organization>/_apis/serviceprincipalentitlements?api-version=7.1-preview.1"
    )


def test_verify_consent_reports_pending_when_admin_has_not_completed_flow() -> None:
    """The read-only Lighthouse probe finds no delegation, so the operator should NOT confirm
    yet — a blind attestation would be premature."""
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_lighthouse_handler_factory(granted=False),
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/verify-consent",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delegationState"] == "pending"
    assert body["consentState"] == "pending"
    assert body["verified"] is False
    assert body["consentCanBeConfirmed"] is False
    assert "admin-consent flow" in body["nextAction"]


def test_verify_consent_reports_granted_and_unlocks_confirm() -> None:
    """Delegation is live but the record is still PENDING — the operator can now confirm with
    evidence instead of a guess."""
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=_lighthouse_handler_factory(granted=True),
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/verify-consent",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["delegationState"] == "granted"
    assert body["consentState"] == "pending"
    assert body["verified"] is True
    assert body["consentCanBeConfirmed"] is True
    assert "onboarding/confirm" in body["nextAction"]
    assert "az deployment sub create" in body["lighthouseCommand"]


def test_verify_consent_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/verify-consent",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 403


def test_verify_consent_404_for_unknown_tenant() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/verify-consent",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 404


def test_grant_ado_org_access_reports_member_pending_pca() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "serviceprincipalentitlements" in url:
            return httpx.Response(201, json={"id": "entitlement"})
        if "connectionData" in url:
            return httpx.Response(403, json={})
        return httpx.Response(404)

    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=handler,
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)
    repo = app.state.tenant_repository
    tenant = asyncio.run(repo.read(TENANT_ID, TENANT_ID))
    assert tenant is not None
    asyncio.run(
        repo.replace(
            TENANT_ID,
            tenant.model_copy(update={"devops_organization_url": ORG_URL}),
        )
    )

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-grant",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "member_pending_pca"
    assert body["verified"] is False
    assert "Project Collection Administrators" in body["pcaInstructionText"]


def test_grant_ado_org_access_fails_clearly_when_control_plane_identity_unknown() -> None:
    """Regression test for the bug found live 2026-09-07: grant_customer_ado_org_access calls
    Azure DevOps *as* the control plane's own identity. A brand-new organization that has never
    heard of that identity rejects even the entitlement call meant to introduce the orchestrator's
    identity, with a 401 that says nothing about which identity is actually the problem. The fix
    self-entitles the control plane's own identity first and, when that fails, returns a distinct
    reason naming the control plane identity specifically."""
    control_plane_id = "88888888-8888-8888-8888-888888888888"

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "serviceprincipalentitlements" in url:
            body = json.loads(request.content)
            origin_id = body["servicePrincipal"]["originId"]
            if origin_id == control_plane_id:
                return httpx.Response(401, json={"message": "TF401444: please sign in"})
            return httpx.Response(201, json={"id": "entitlement"})
        return httpx.Response(404)

    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=handler,
        controlplane_principal_id=control_plane_id,
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)
    repo = app.state.tenant_repository
    tenant = asyncio.run(repo.read(TENANT_ID, TENANT_ID))
    assert tenant is not None
    asyncio.run(
        repo.replace(
            TENANT_ID,
            tenant.model_copy(update={"devops_organization_url": ORG_URL}),
        )
    )

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-grant",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "error"
    assert body["reason"] == "control_plane_identity_not_recognised"
    assert body["evidence"]["controlPlanePrincipalObjectId"] == control_plane_id
    assert "control plane" in body["next_action"].lower()


def test_grant_ado_org_access_reports_member_when_probe_passes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "serviceprincipalentitlements" in url:
            return httpx.Response(201, json={"id": "entitlement"})
        if "connectionData" in url:
            return httpx.Response(200, json={"instanceId": ORGANIZATION_GUID})
        return httpx.Response(404)

    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        http_handler=handler,
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)
    repo = app.state.tenant_repository
    tenant = asyncio.run(repo.read(TENANT_ID, TENANT_ID))
    assert tenant is not None
    asyncio.run(
        repo.replace(
            TENANT_ID,
            tenant.model_copy(update={"devops_organization_url": ORG_URL}),
        )
    )

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-grant",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "member"
    assert body["verified"] is True


def test_grant_ado_org_access_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/ado-access-grant",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 403


def test_lighthouse_onboarding_is_503_when_arm_credential_unconfigured() -> None:
    class _BrokenCredential:
        async def get_token(self, *scopes: str, **kwargs: Any) -> Any:
            raise ValueError("not configured")

    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        credential=_BrokenCredential(),
    )
    client = TestClient(app)
    _create_tenant_with_subscription(client)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/lighthouse",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/problem+json"


def test_lighthouse_onboarding_unknown_tenant_is_404() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)

    response = client.get(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/onboarding/lighthouse",
        headers={"Authorization": "Bearer good-token"},
    )

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"


def test_lighthouse_onboarding_unknown_tenant_logs_a_detective_signal(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """WAF assessment §2.6: an operator probing tenant_ids that don't exist is exactly what
    enumeration (insider misuse, or a compromised operator token) looks like. Narrowing the 404
    itself would not close the same oracle available via the canonical GET /v1/tenants/{id}, but a
    WARNING log line is a real detective control an alert rule can act on."""
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))},
        credential=_FakeArmCredential(),
    )
    client = TestClient(app)
    unknown_tenant_id = "99999999-9999-9999-9999-999999999999"

    with caplog.at_level("WARNING"):
        response = client.get(
            f"/v1/tenants/{unknown_tenant_id}/onboarding/lighthouse",
            headers={"Authorization": "Bearer good-token"},
        )

    assert response.status_code == 404
    assert any(
        "lighthouse onboarding facts requested for unknown tenant_id" in record.message
        and unknown_tenant_id in record.message
        and OPERATOR_ID in record.message
        for record in caplog.records
    )


def test_offshore_inference_consent_requires_authentication() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post("/v1/tenants/offshore-inference-consent", json={})

    assert response.status_code == 401, response.text


def test_offshore_inference_consent_persists_voice_identical_shape() -> None:
    app, _container, consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/offshore-inference-consent",
        headers={"Authorization": "Bearer good-token"},
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "consenting_identity_object_id": OPERATOR_ID,
        "consenting_identity_display_name": f"Test Caller {OPERATOR_ID[:8]}",
        "consented_at": NOW.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artefact_uri": body["artefact_uri"],
        "disclosure_version": CURRENT_DISCLOSURE_VERSION,
    }
    assert body["artefact_uri"].startswith("https://example.invalid/consent/")
    assert consent_store.calls == [
        {
            "consent_id": consent_store.calls[0]["consent_id"],
            "consenting_identity_object_id": OPERATOR_ID,
            "consenting_identity_display_name": f"Test Caller {OPERATOR_ID[:8]}",
            "disclosure_version": CURRENT_DISCLOSURE_VERSION,
            "now": NOW,
        }
    ]


def test_offshore_inference_consent_rejects_disclosure_version_mismatch() -> None:
    app, _container, consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/offshore-inference-consent",
        headers={"Authorization": "Bearer good-token"},
        json={"disclosureVersion": "9.9.9"},
    )

    assert response.status_code == 409, response.text
    assert "disclosureVersion does not match" in response.json()["detail"]
    assert consent_store.calls == []


def test_offshore_inference_consent_attaches_to_existing_tenant() -> None:
    """The gap this closes: recording the consent artefact used to be disconnected from the
    tenant record VoiceEnablementGate actually reads. See attach_offshore_inference_consent."""
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    created = client.post("/v1/tenants", headers=headers, json=_create_body())
    assert created.status_code == 201, created.text
    assert created.json()["offshoreInferenceConsent"] is None

    consented = client.post(
        "/v1/tenants/offshore-inference-consent",
        headers=headers,
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )
    assert consented.status_code == 201, consented.text

    refetched = client.get(f"/v1/tenants/{TENANT_ID}", headers=headers)
    assert refetched.status_code == 200, refetched.text
    attached = refetched.json()["offshoreInferenceConsent"]
    assert attached is not None
    assert attached["consentingIdentityObjectId"] == OPERATOR_ID
    assert attached["disclosureVersion"] == CURRENT_DISCLOSURE_VERSION


def test_offshore_inference_consent_with_no_tenant_yet_does_not_error() -> None:
    """A caller consenting before formal onboarding still gets a durable artefact — see
    attach_offshore_inference_consent's docstring for why returning None here is correct."""
    app, _container, consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/offshore-inference-consent",
        headers={"Authorization": "Bearer good-token"},
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )

    assert response.status_code == 201, response.text
    assert len(consent_store.calls) == 1


def test_set_voice_channel_enabled_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers={"Authorization": "Bearer good-token"},
        json={"enabled": True, "note": "sold with voice per SOW-1"},
    )

    assert response.status_code == 403, response.text


def test_set_voice_channel_enabled_requires_existing_tenant() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers={"Authorization": "Bearer good-token"},
        json={"enabled": True, "note": "sold with voice per SOW-1"},
    )

    assert response.status_code == 404, response.text


def test_set_voice_channel_enabled_toggles_flag() -> None:
    # CustomerTenant's own validator forbids voice_channel_enabled=True without consent already
    # recorded (FR-053d) — consent has to land first, matching the real required sequence.
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}
    client.post("/v1/tenants", headers=headers, json=_create_body())
    client.post(
        "/v1/tenants/offshore-inference-consent",
        headers=headers,
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )

    enabled = client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers=headers,
        json={"enabled": True, "note": "sold with voice per SOW-1"},
    )
    assert enabled.status_code == 200, enabled.text
    assert enabled.json()["voiceChannelEnabled"] is True

    disabled = client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers=headers,
        json={"enabled": False, "note": "customer opted out of voice, ticket #123"},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["voiceChannelEnabled"] is False


def test_add_subscription_entitlement_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions",
        headers={"Authorization": "Bearer good-token"},
        json={
            "subscriptionId": "22222222-2222-2222-2222-222222222222",
            "displayName": "Customer subscription",
            "mayDeploy": True,
            "note": "signed SOW covers this subscription",
        },
    )

    assert response.status_code == 403, response.text


def test_add_subscription_entitlement_requires_existing_tenant() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions",
        headers={"Authorization": "Bearer good-token"},
        json={
            "subscriptionId": "22222222-2222-2222-2222-222222222222",
            "displayName": "Customer subscription",
            "mayDeploy": True,
            "note": "signed SOW covers this subscription",
        },
    )

    assert response.status_code == 404, response.text


def test_add_subscription_entitlement_records_it() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}
    client.post("/v1/tenants", headers=headers, json=_create_body())

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/subscriptions",
        headers=headers,
        json={
            "subscriptionId": "22222222-2222-2222-2222-222222222222",
            "displayName": "Customer subscription",
            "mayDeploy": True,
            "note": "signed SOW covers this subscription",
        },
    )

    assert response.status_code == 201, response.text
    subscriptions = response.json()["subscriptions"]
    assert len(subscriptions) == 1
    assert subscriptions[0]["subscriptionId"] == "22222222-2222-2222-2222-222222222222"
    assert subscriptions[0]["displayName"] == "Customer subscription"
    assert subscriptions[0]["mayDeploy"] is True
    assert subscriptions[0]["bootstrapIdentityResourceId"] is None


def test_add_subscription_entitlement_rejects_duplicate() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}
    client.post("/v1/tenants", headers=headers, json=_create_body())
    body = {
        "subscriptionId": "22222222-2222-2222-2222-222222222222",
        "displayName": "Customer subscription",
        "mayDeploy": True,
        "note": "signed SOW covers this subscription",
    }
    first = client.post(f"/v1/tenants/{TENANT_ID}/subscriptions", headers=headers, json=body)
    assert first.status_code == 201, first.text

    second = client.post(f"/v1/tenants/{TENANT_ID}/subscriptions", headers=headers, json=body)
    assert second.status_code == 409, second.text


def test_full_onboarding_and_consent_flow_enables_voice() -> None:
    """End-to-end regression for the wiring gap this session found: a tenant that goes through
    every real onboarding + consent + voice-enablement route ends up with
    CustomerTenant.may_accept_voice_call() true - not just each individual route returning 2xx.

    Order matters and is itself part of what this test documents: consent has to be recorded
    before voice_channel_enabled can be set True, per CustomerTenant's own validator (FR-053d) -
    enabling the toggle first, as a sales-lead-in step, is not a valid sequence."""
    app, container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    client.post("/v1/tenants", headers=headers, json=_create_body())
    confirmed = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers=headers,
        json={"note": "confirmed by customer admin via email"},
    )
    assert confirmed.status_code == 200, confirmed.text
    consented = client.post(
        "/v1/tenants/offshore-inference-consent",
        headers=headers,
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )
    assert consented.status_code == 201, consented.text
    voice_on = client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers=headers,
        json={"enabled": True, "note": "sold with voice per SOW-1"},
    )
    assert voice_on.status_code == 200, voice_on.text

    document = container._items[(TENANT_ID, TENANT_ID)]
    tenant = from_document(CustomerTenant, document)
    assert tenant.may_accept_voice_call() is True


# ---------------------------------------------------------------------------
# Onboarding status endpoint tests
# ---------------------------------------------------------------------------


def test_onboarding_status_shows_pending_steps_for_new_tenant() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    client.post("/v1/tenants", headers=headers, json=_create_body())

    response = client.get(f"/v1/tenants/{TENANT_ID}/onboarding/status", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["tenantId"] == TENANT_ID
    assert body["consentState"] == "pending"
    assert body["steps"]["tenantCreated"]["completed"] is True
    assert body["steps"]["consentConfirmed"]["completed"] is False
    assert body["steps"]["offshoreConsentRecorded"]["completed"] is False
    assert body["steps"]["voiceEnabled"]["completed"] is False
    assert "onboarding/confirm" in body["nextAction"]


def test_onboarding_status_guides_next_action_after_consent() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    client.post("/v1/tenants", headers=headers, json=_create_body())
    confirmed = client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers=headers,
        json={"note": "confirmed by customer admin via email"},
    )
    assert confirmed.status_code == 200, confirmed.text

    response = client.get(f"/v1/tenants/{TENANT_ID}/onboarding/status", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["consentState"] == "granted"
    assert body["steps"]["consentConfirmed"]["completed"] is True
    assert body["steps"]["offshoreConsentRecorded"]["completed"] is False
    assert "offshore-inference-consent" in body["nextAction"]


def test_onboarding_status_complete_when_all_done() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    client.post("/v1/tenants", headers=headers, json=_create_body())
    client.post(
        f"/v1/tenants/{TENANT_ID}/onboarding/confirm",
        headers=headers,
        json={"note": "confirmed by customer admin via email"},
    )
    client.post(
        "/v1/tenants/offshore-inference-consent",
        headers=headers,
        json={"disclosureVersion": CURRENT_DISCLOSURE_VERSION},
    )
    client.post(
        f"/v1/tenants/{TENANT_ID}/voice-channel",
        headers=headers,
        json={"enabled": True, "note": "sold with voice per SOW-1"},
    )

    response = client.get(f"/v1/tenants/{TENANT_ID}/onboarding/status", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["steps"]["tenantCreated"]["completed"] is True
    assert body["steps"]["consentConfirmed"]["completed"] is True
    assert body["steps"]["offshoreConsentRecorded"]["completed"] is True
    assert body["steps"]["voiceEnabled"]["completed"] is True
    assert "complete" in body["nextAction"].lower()


def test_onboarding_status_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/status",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 403


def test_onboarding_status_404_for_unknown_tenant() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.get(
        f"/v1/tenants/{TENANT_ID}/onboarding/status",
        headers={"Authorization": "Bearer good-token"},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Quick-onboard endpoint tests
# ---------------------------------------------------------------------------


def _quick_onboard_body(**overrides: Any) -> dict[str, Any]:
    body = {
        "tenantId": TENANT_ID,
        "displayName": "Test Customer",
        "approvedRegions": ["australiaeast"],
        "dataResidencyRegions": ["australiaeast"],
        "consentNote": "own dev tenant, self-confirmed",
    }
    body.update(overrides)
    return body


def test_quick_onboard_completes_all_steps() -> None:
    app, container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    response = client.post(
        "/v1/tenants/onboarding/quick-onboard",
        headers=headers,
        json=_quick_onboard_body(
            voiceEnabled=True,
            voiceNote="voice included in this engagement",
        ),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["steps"]["tenantCreated"]["completed"] is True
    assert body["steps"]["consentConfirmed"]["completed"] is True
    assert body["steps"]["offshoreConsentRecorded"]["completed"] is True
    assert body["steps"]["voiceEnabled"]["completed"] is True
    assert body["consentState"] == "granted"
    assert "complete" in body["nextAction"].lower()

    # Verify the underlying tenant record is fully onboarded.
    document = container._items[(TENANT_ID, TENANT_ID)]
    tenant = from_document(CustomerTenant, document)
    assert tenant.may_accept_voice_call() is True
    assert tenant.consent_confirmed_by_object_id == OPERATOR_ID
    assert tenant.consent_confirmation_note == "own dev tenant, self-confirmed"


def test_quick_onboard_without_voice_leaves_voice_disabled() -> None:
    app, container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)
    headers = {"Authorization": "Bearer good-token"}

    response = client.post(
        "/v1/tenants/onboarding/quick-onboard",
        headers=headers,
        json=_quick_onboard_body(),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["steps"]["consentConfirmed"]["completed"] is True
    assert body["steps"]["offshoreConsentRecorded"]["completed"] is True
    assert body["steps"]["voiceEnabled"]["completed"] is False

    document = container._items[(TENANT_ID, TENANT_ID)]
    tenant = from_document(CustomerTenant, document)
    assert tenant.consent_state == ConsentState.GRANTED
    assert tenant.offshore_inference_consent is not None
    assert tenant.voice_channel_enabled is False


def test_quick_onboard_requires_operator_role() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/onboarding/quick-onboard",
        headers={"Authorization": "Bearer good-token"},
        json=_quick_onboard_body(),
    )
    assert response.status_code == 403


def test_quick_onboard_rejects_mismatched_tenant() -> None:
    """quick-onboard is only for the operator's own tenant (caller tid must match tenantId)."""
    caller = _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))
    caller = AuthenticatedCaller(
        object_id=caller.object_id,
        tenant_id="99999999-9999-9999-9999-999999999999",
        display_name=caller.display_name,
        roles=caller.roles,
        token_expires_at=caller.token_expires_at,
    )
    app, _container, _consent_store = _build_app(
        callers={"good-token": caller}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/onboarding/quick-onboard",
        headers={"Authorization": "Bearer good-token"},
        json=_quick_onboard_body(),
    )
    assert response.status_code == 403


def test_quick_onboard_requires_voice_note_when_enabled() -> None:
    app, _container, _consent_store = _build_app(
        callers={"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/onboarding/quick-onboard",
        headers={"Authorization": "Bearer good-token"},
        json=_quick_onboard_body(voiceEnabled=True),
    )
    assert response.status_code == 400
