"""Multi-tenant token trust (added 2026-08-25, FR-006 cross-tenant customers).

Covers the two new pieces: :class:`MultiTenantTokenDecoder`'s tid-based routing to per-tenant
verifiers, and ``TokenPolicy``'s onboarded-tenant issuer expansion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt
import pytest

from groundwork_controlplane.api.auth import TokenPolicy, TokenValidator
from groundwork_controlplane.api.entra_decoder import MultiTenantTokenDecoder

HOME = "11111111-1111-1111-1111-111111111111"
CUSTOMER = "22222222-2222-2222-2222-222222222222"
ROGUE = "33333333-3333-3333-3333-333333333333"


class _RecordingDecoder:
    """Stands in for EntraTokenDecoder: records which tenant's verifier was selected."""

    def __init__(self, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.calls = 0

    def decode(self, token: str) -> dict[str, object]:
        self.calls += 1
        return {"tid": self.tenant_id, "oid": "o", "exp": 9999999999}


@pytest.fixture()
def multi_decoder(monkeypatch: pytest.MonkeyPatch) -> MultiTenantTokenDecoder:
    def fake_init(self: Any, tenant_id: str) -> None:
        self.tenant_id = tenant_id
        self.calls = 0

    def fake_decode(self: Any, token: str) -> dict[str, object]:
        self.calls += 1
        return {"tid": self.tenant_id, "oid": "o", "exp": 9999999999}

    monkeypatch.setattr(
        "groundwork_controlplane.api.entra_decoder.EntraTokenDecoder.__init__", fake_init
    )
    monkeypatch.setattr(
        "groundwork_controlplane.api.entra_decoder.EntraTokenDecoder.decode", fake_decode
    )
    decoder = MultiTenantTokenDecoder(home_tenant_id=HOME)
    decoder.register_tenant(CUSTOMER)
    return decoder


def test_routes_to_customer_tenant_verifier(multi_decoder: MultiTenantTokenDecoder) -> None:
    token = jwt.encode({"tid": CUSTOMER}, "k", algorithm="HS256")
    claims = multi_decoder.decode(token)
    assert claims["tid"] == CUSTOMER


def test_unknown_tenant_falls_back_to_home_verifier(multi_decoder: MultiTenantTokenDecoder) -> None:
    token = jwt.encode({"tid": ROGUE}, "k", algorithm="HS256")
    claims = multi_decoder.decode(token)
    # The rogue tid never gets its own verifier — it is verified against home keys.
    assert claims["tid"] == HOME


def test_register_tenant_is_idempotent(multi_decoder: MultiTenantTokenDecoder) -> None:
    before = multi_decoder.known_tenants()
    multi_decoder.register_tenant(CUSTOMER)
    assert multi_decoder.known_tenants() == before


def test_policy_add_tenant_expands_both_issuer_forms() -> None:
    policy = TokenPolicy(expected_audience="api://x", allowed_issuers=frozenset({"home"}))
    policy.add_tenant(CUSTOMER)
    assert f"https://login.microsoftonline.com/{CUSTOMER}/v2.0" in policy.allowed_issuers
    assert f"https://sts.windows.net/{CUSTOMER}/" in policy.allowed_issuers
    assert "home" in policy.allowed_issuers


def test_validator_accepts_onboarded_customer_issuer() -> None:
    class _Decoder:
        def decode(self, token: str) -> dict[str, object]:
            return {
                "tid": CUSTOMER,
                "oid": "o-1",
                "iss": f"https://login.microsoftonline.com/{CUSTOMER}/v2.0",
                "aud": "api://x",
                "exp": int((datetime.now(tz=UTC) + timedelta(hours=1)).timestamp()),
                "roles": ["Groundwork.Requester"],
            }

    policy = TokenPolicy(
        expected_audience="api://x",
        allowed_issuers=frozenset({"https://login.microsoftonline.com/HOME/v2.0"}),
        require_role=True,
    )
    policy.add_tenant(CUSTOMER)
    validator = TokenValidator(decoder=_Decoder(), policy=policy)

    caller = validator.validate("Bearer fake-token")
    assert caller.tenant_id == CUSTOMER


def test_validator_rejects_unonboarded_tenant_issuer() -> None:
    class _Decoder:
        def decode(self, token: str) -> dict[str, object]:
            return {
                "tid": ROGUE,
                "oid": "o-1",
                "iss": f"https://login.microsoftonline.com/{ROGUE}/v2.0",
                "aud": "api://x",
                "exp": int((datetime.now(tz=UTC) + timedelta(hours=1)).timestamp()),
                "roles": ["Groundwork.Requester"],
            }

    policy = TokenPolicy(
        expected_audience="api://x",
        allowed_issuers=frozenset({"https://login.microsoftonline.com/HOME/v2.0"}),
    )
    validator = TokenValidator(decoder=_Decoder(), policy=policy)

    with pytest.raises(Exception, match="issuer"):
        validator.validate("Bearer fake-token")


def test_refresh_cross_tenant_auth_registers_and_trusts_new_tenants() -> None:
    from groundwork_controlplane.api.auth import TokenPolicy, refresh_cross_tenant_auth

    registered: list[str] = []

    def register(tenant_id: str) -> None:
        registered.append(tenant_id)

    policy = TokenPolicy(
        expected_audience="api://groundwork",
        allowed_issuers=frozenset({"https://login.microsoftonline.com/home-tenant/v2.0"}),
    )

    newly = refresh_cross_tenant_auth(register, policy, ["tenant-a", "home-tenant", "tenant-b"])

    assert set(registered) == {"tenant-a", "home-tenant", "tenant-b"}
    assert newly == 2  # home tenant's issuers were already trusted
    for tid in ("tenant-a", "tenant-b"):
        assert f"https://login.microsoftonline.com/{tid}/v2.0" in policy.allowed_issuers
        assert f"https://sts.windows.net/{tid}/" in policy.allowed_issuers


def test_refresh_cross_tenant_auth_is_idempotent() -> None:
    from groundwork_controlplane.api.auth import TokenPolicy, refresh_cross_tenant_auth

    policy = TokenPolicy(
        expected_audience="api://groundwork",
        allowed_issuers=frozenset({"https://login.microsoftonline.com/home/v2.0"}),
    )
    register = lambda tid: None  # noqa: E731

    refresh_cross_tenant_auth(register, policy, ["t-1"])
    size_after_first = len(policy.allowed_issuers)
    newly = refresh_cross_tenant_auth(register, policy, ["t-1"])

    assert newly == 0
    assert len(policy.allowed_issuers) == size_after_first
