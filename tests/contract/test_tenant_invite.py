"""Tenant email invite (``POST /v1/tenants/{id}/invite``) — gating and delivery contract.

The invite route packages the real admin-consent URL into an ACS Email send. What's under
test here: operator gating, the built-but-not-configured 503, tenant 404, and that the email
actually carries the consent URL for the right tenant with the configured sender. The sender
is faked at the ``build_email_sender`` seam (the same injectable-factory discipline as every
other Azure SDK touchpoint in this codebase).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.tenant import CustomerTenant
from groundwork_controlplane.api.auth import AuthenticatedCaller, AuthenticationError, CallerRole
from groundwork_controlplane.api.errors import register_error_handlers
from groundwork_controlplane.api.tenants import router as tenants_router
from groundwork_orchestrator.state.cosmos import TenantScopedRepository
from groundwork_shared.config.settings import EntraSettings
from groundwork_shared.notify.dispatcher import NotificationDispatchError

pytestmark = pytest.mark.contract

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OPERATOR_ID = "77777777-7777-7777-7777-777777777777"
REQUESTER_ID = "44444444-4444-4444-4444-444444444444"


class _FakeContainer:
    def __init__(self) -> None:
        self._items: dict[tuple[str, str], dict[str, Any]] = {}

    def seed(self, tenant_id: str, item_id: str, body: dict[str, Any]) -> None:
        self._items[(tenant_id, item_id)] = body

    async def create_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> dict[str, Any]:
        self._items[(body["tenantId"], body["id"])] = body
        return body

    def query_items(self, query: str, *, parameters: Any = None, **_: Any):  # pragma: no cover
        raise AssertionError("invite tests never query containers")

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> dict[str, Any]:
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        found = self._items.get((partition_key, item))
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found


class _FakeTokenValidator:
    def __init__(self, callers: dict[str, AuthenticatedCaller]) -> None:
        self._callers = callers

    def validate(self, authorization_header: str | None) -> AuthenticatedCaller:
        token = (authorization_header or "").removeprefix("Bearer ")
        caller = self._callers.get(token)
        if caller is None:
            raise AuthenticationError("unknown or missing token")
        return caller


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self._fail = fail

    async def send(self, **kwargs: Any) -> str:
        if self._fail:
            raise NotificationDispatchError({"code": "test", "message": "smtp exploded"})
        self.calls.append(kwargs)
        return "fake-email-operation-id"


def _caller(object_id: str, *, roles: tuple[CallerRole, ...]) -> AuthenticatedCaller:
    return AuthenticatedCaller(
        object_id=object_id,
        tenant_id=TENANT_ID,
        display_name=f"Test Caller {object_id[:8]}",
        roles=frozenset(roles),
        token_expires_at=datetime(2099, 1, 1, tzinfo=UTC),
    )


def _build_app(
    *,
    configured: bool = True,
    callers: dict[str, AuthenticatedCaller] | None = None,
) -> FastAPI:
    import groundwork_controlplane.api.tenants as tenants_module

    app = FastAPI()
    app.include_router(tenants_router)
    register_error_handlers(app)

    container = _FakeContainer()
    tenant = CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Customer",
        consent_state=__import__(
            "groundwork_contracts.tenant", fromlist=["ConsentState"]
        ).ConsentState.PENDING,
        consent_granted_at=None,
        subscriptions=(),
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
        concurrency_cap=3,
    )
    container.seed(
        TENANT_ID,
        TENANT_ID,
        tenant.model_dump(mode="json") | {"id": TENANT_ID, "tenantId": TENANT_ID},
    )
    app.state.tenant_repository = TenantScopedRepository(
        container, model_cls=CustomerTenant, id_field="tenant_id"
    )
    app.state.token_validator = _FakeTokenValidator(
        callers
        if callers is not None
        else {"good-token": _caller(OPERATOR_ID, roles=(CallerRole.OPERATOR,))}
    )
    app.state.now_fn = lambda: NOW
    app.state.credential = object()  # sentinel; the faked factory never uses it
    app.state.settings = type(
        "FakeSettings",
        (),
        {
            "entra": EntraSettings(
                tenant_id="provider-tenant",
                client_id="00000000-0000-0000-0000-000000000000",
                audience="api://00000000-0000-0000-0000-000000000000",
                redirect_uri="https://portal.azure.com",
            ),
            "acs_email_endpoint": (
                "https://acs.example.communication.azure.com" if configured else ""
            ),
            "acs_email_sender_address": ("DoNotReply@example.azurecomm.net" if configured else ""),
        },
    )()

    fake_sender = _FakeSender()
    tenants_module.build_email_sender = (  # type: ignore[assignment]
        lambda **_: fake_sender
    )
    app.state.fake_sender = fake_sender
    return app


NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _invite_body() -> dict[str, Any]:
    return {"adminEmail": "admin@customer.example"}


def test_invite_requires_operator_role() -> None:
    app = _build_app(callers={"good-token": _caller(REQUESTER_ID, roles=(CallerRole.REQUESTER,))})
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/invite",
        headers={"Authorization": "Bearer good-token"},
        json=_invite_body(),
    )

    assert response.status_code == 403, response.text


def test_invite_returns_503_when_email_unconfigured() -> None:
    app = _build_app(configured=False)
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/invite",
        headers={"Authorization": "Bearer good-token"},
        json=_invite_body(),
    )

    assert response.status_code == 503, response.text
    assert "GROUNDWORK_ACS_EMAIL_ENDPOINT" in response.json()["detail"]


def test_invite_unknown_tenant_404() -> None:
    app = _build_app()
    client = TestClient(app)

    response = client.post(
        "/v1/tenants/99999999-9999-9999-9999-999999999999/invite",
        headers={"Authorization": "Bearer good-token"},
        json=_invite_body(),
    )

    assert response.status_code == 404, response.text


def test_invite_sends_consent_url_via_configured_sender() -> None:
    app = _build_app()
    client = TestClient(app)
    fake_sender: _FakeSender = app.state.fake_sender

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/invite",
        headers={"Authorization": "Bearer good-token"},
        json={"adminEmail": "admin@customer.example", "adminDisplayName": "Customer Admin"},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert body["tenantId"] == TENANT_ID
    assert body["adminEmail"] == "admin@customer.example"
    assert "adminconsent" in body["consentUrl"]
    assert TENANT_ID in body["consentUrl"]
    assert body["emailOperationId"] == "fake-email-operation-id"

    call = fake_sender.calls[0]
    assert call["to_address"] == "admin@customer.example"
    assert call["to_display_name"] == "Customer Admin"
    assert "Test Customer" in call["subject"]
    assert "adminconsent" in call["html"]
    assert "adminconsent" in call["plain_text"]


def test_invite_sender_failure_maps_to_502() -> None:
    app = _build_app()
    # Re-wire the faked factory to a failing sender.
    failing = _FakeSender(fail=True)
    import groundwork_controlplane.api.tenants as tenants_module

    tenants_module.build_email_sender = lambda **_: failing  # type: ignore[assignment]
    client = TestClient(app)

    response = client.post(
        f"/v1/tenants/{TENANT_ID}/invite",
        headers={"Authorization": "Bearer good-token"},
        json=_invite_body(),
    )

    assert response.status_code == 502, response.text
