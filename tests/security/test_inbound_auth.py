"""T021b — inbound token validation must fail closed.

FR-005 requires every requester to be authenticated before any tenant metadata is read. FR-007
requires authority to derive from the session and nothing else.

The most important test here is ``test_tenant_comes_from_the_token_not_the_request``: it asserts the
property that makes conversational input safe. If the tenant could ever come from request content, a
prompt-injection attack would only need to persuade the agent to name a different tenant.

A fake decoder is used rather than a real Entra tenant so claim policy can be exercised
exhaustively. Signature verification is the decoder's job and is not re-tested here — it is
delegated to a library precisely so it is not our cryptography to get wrong.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from groundwork_controlplane.api.auth import (
    AuthenticationError,
    AuthorizationError,
    CallerRole,
    TokenPolicy,
    TokenValidator,
)

pytestmark = pytest.mark.security

TENANT_ID = "11111111-1111-1111-1111-111111111111"
OTHER_TENANT_ID = "22222222-2222-2222-2222-222222222222"
OBJECT_ID = "44444444-4444-4444-4444-444444444444"

AUDIENCE = "api://groundwork"
ISSUER = f"https://login.microsoftonline.com/{TENANT_ID}/v2.0"

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


class FakeDecoder:
    """Returns preset claims, or raises to simulate a cryptographic failure."""

    def __init__(self, claims: dict[str, object] | None = None, *, fail: bool = False) -> None:
        self._claims = claims or {}
        self._fail = fail

    def decode(self, token: str) -> dict[str, object]:
        if self._fail:
            raise ValueError("signature verification failed")
        return self._claims


def _claims(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "aud": AUDIENCE,
        "iss": ISSUER,
        "tid": TENANT_ID,
        "oid": OBJECT_ID,
        "name": "Platform Admin",
        "roles": ["Groundwork.Requester"],
        "exp": (NOW + timedelta(hours=1)).timestamp(),
    }
    base.update(overrides)
    return base


def _validator(claims: dict[str, object] | None = None, *, fail: bool = False) -> TokenValidator:
    return TokenValidator(
        decoder=FakeDecoder(claims if claims is not None else _claims(), fail=fail),
        policy=TokenPolicy(expected_audience=AUDIENCE, allowed_issuers=frozenset({ISSUER})),
    )


def test_valid_token_produces_a_caller() -> None:
    caller = _validator().validate("Bearer token", now=NOW)

    assert caller.tenant_id == TENANT_ID
    assert caller.object_id == OBJECT_ID
    assert CallerRole.REQUESTER in caller.roles


def test_tenant_comes_from_the_token_not_the_request() -> None:
    """FR-007 — the single most important property in this module.

    ``AuthenticatedCaller`` has no constructor accepting a tenant from request content. The only
    path to one is a validated ``tid`` claim, so conversation content cannot redirect the target.
    """
    caller = _validator(_claims(tid=OTHER_TENANT_ID)).validate("Bearer t", now=NOW)

    assert caller.tenant_id == OTHER_TENANT_ID  # from the claim, not from anything a caller sent


@pytest.mark.parametrize(
    "header",
    [None, "", "token-without-scheme", "Basic dXNlcjpwYXNz", "Bearer", "Bearer   "],
)
def test_malformed_authorization_header_is_rejected(header: str | None) -> None:
    with pytest.raises(AuthenticationError):
        _validator().validate(header, now=NOW)


def test_failed_signature_is_rejected() -> None:
    """Any decoder failure is authentication failure. There is no partial trust."""
    with pytest.raises(AuthenticationError, match="cryptographic validation"):
        _validator(fail=True).validate("Bearer forged", now=NOW)


def test_wrong_audience_is_rejected() -> None:
    """A token minted for another API must not be accepted here."""
    with pytest.raises(AuthenticationError, match="audience"):
        _validator(_claims(aud="api://some-other-service")).validate("Bearer t", now=NOW)


def test_audience_list_form_is_supported() -> None:
    """Entra presents `aud` as a list for some token types."""
    caller = _validator(_claims(aud=[AUDIENCE, "api://other"])).validate("Bearer t", now=NOW)

    assert caller.tenant_id == TENANT_ID


def test_untrusted_issuer_is_rejected() -> None:
    with pytest.raises(AuthenticationError, match="issuer"):
        _validator(_claims(iss="https://evil.invalid/v2.0")).validate("Bearer t", now=NOW)


def test_expired_token_is_rejected_independently_of_the_decoder() -> None:
    """A second expiry check, so a misconfigured decoder cannot silently admit stale tokens."""
    expired = _claims(exp=(NOW - timedelta(minutes=1)).timestamp())

    with pytest.raises(AuthenticationError, match="expired"):
        _validator(expired).validate("Bearer t", now=NOW)


def test_missing_tenant_claim_is_rejected() -> None:
    claims = _claims()
    del claims["tid"]

    with pytest.raises(AuthenticationError, match="tenant claim"):
        _validator(claims).validate("Bearer t", now=NOW)


def test_missing_object_id_is_rejected() -> None:
    """Without an object id the action cannot be attributed, which FR-047 requires."""
    claims = _claims()
    del claims["oid"]

    with pytest.raises(AuthenticationError, match="object identifier"):
        _validator(claims).validate("Bearer t", now=NOW)


def test_missing_expiry_is_rejected() -> None:
    claims = _claims()
    del claims["exp"]

    with pytest.raises(AuthenticationError, match="expiry"):
        _validator(claims).validate("Bearer t", now=NOW)


def test_token_without_recognised_role_is_rejected() -> None:
    with pytest.raises(AuthenticationError, match="application role"):
        _validator(_claims(roles=["SomeOtherApp.Reader"])).validate("Bearer t", now=NOW)


def test_unrecognised_roles_are_ignored_not_fatal() -> None:
    """Entra tokens carry roles for other applications; rejecting them would couple us to them."""
    caller = _validator(_claims(roles=["SomeOtherApp.Reader", "Groundwork.Approver"])).validate(
        "Bearer t", now=NOW
    )

    assert caller.roles == frozenset({CallerRole.APPROVER})


def test_role_requirement_is_enforced() -> None:
    caller = _validator().validate("Bearer t", now=NOW)

    caller.require_role(CallerRole.REQUESTER)
    with pytest.raises(AuthorizationError, match=r"Groundwork\.Approver"):
        caller.require_role(CallerRole.APPROVER)


def test_approver_capability_is_role_derived() -> None:
    """FR-020a — approval authority comes from the token's role, not from a request field."""
    requester = _validator().validate("Bearer t", now=NOW)
    approver = _validator(_claims(roles=["Groundwork.Approver"])).validate("Bearer t", now=NOW)

    assert requester.may_approve() is False
    assert approver.may_approve() is True


def test_caller_is_immutable() -> None:
    """A validated identity must not be mutable by downstream code."""
    caller = _validator().validate("Bearer t", now=NOW)

    with pytest.raises(AttributeError):
        caller.tenant_id = OTHER_TENANT_ID  # type: ignore[misc]


def test_authentication_error_does_not_leak_which_check_failed_in_detail() -> None:
    """Coarse reasons only: precise failure detail helps an attacker more than a legitimate user."""
    with pytest.raises(AuthenticationError) as excinfo:
        _validator(_claims(iss="https://evil.invalid/v2.0")).validate("Bearer t", now=NOW)

    # Names the category, but not the expected issuer value.
    assert "evil.invalid" not in str(excinfo.value)
    assert ISSUER not in str(excinfo.value)
