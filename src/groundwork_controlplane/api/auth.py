"""Inbound Entra ID token validation — T021a, FR-005, FR-007.

Specification review found this missing: the specification says "authenticated administrator"
throughout, the plan inherited it as a premise, and nothing produced it. Every user story depended
on a layer that did not exist.

The load-bearing rule is FR-007: **authority derives from the validated token and nothing else.**
The tenant a caller may act in comes from the ``tid`` claim, never from a request body, a header a
caller controls, or anything said in a conversation. That is why :class:`AuthenticatedCaller` has no
constructor taking a tenant id — the only way to get one is to validate a token.

Signature verification is deliberately not hand-rolled. It requires JWKS retrieval, key rotation,
and issuer discovery; ``python-jose``/``PyJWT`` with ``PyJWKClient`` handle that, and
"do not implement custom cryptography" is a standing rule. This module owns claim policy — audience,
issuer, expiry, tenant extraction — and delegates the cryptography.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol


class AuthenticationError(Exception):
    """A token was absent, malformed, expired, or failed validation.

    Always maps to HTTP 401. Deliberately carries no detail about *why* beyond a coarse reason —
    telling an unauthenticated caller whether the issuer or the audience was wrong helps them
    more than it helps us.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class AuthorizationError(Exception):
    """The caller is authenticated but not entitled to the requested scope.

    Maps to HTTP 403. Distinct from :class:`AuthenticationError` because the remediation differs:
    one means sign in, the other means ask for access.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


class CallerRole(StrEnum):
    """Application roles carried in the token's ``roles`` claim.

    Deliberately coarse. Fine-grained entitlement is a property of the tenant record
    (``CustomerTenant.subscriptions``), not of the token, so that revoking access to one
    subscription does not require reissuing tokens.
    """

    REQUESTER = "Groundwork.Requester"
    APPROVER = "Groundwork.Approver"
    OPERATOR = "Groundwork.Operator"


@dataclass(frozen=True, slots=True)
class AuthenticatedCaller:
    """The result of successfully validating a bearer token.

    Every field originates from a verified claim. There is no code path that constructs this from
    request content, which is what makes FR-007 structurally true rather than a rule to remember.
    """

    object_id: str
    tenant_id: str
    display_name: str
    roles: frozenset[CallerRole]
    token_expires_at: datetime
    authentication_methods: frozenset[str] = frozenset()
    token_issued_at: datetime | None = None

    def require_role(self, role: CallerRole) -> None:
        """Raise :class:`AuthorizationError` unless the caller holds ``role``."""
        if role not in self.roles:
            raise AuthorizationError(
                f"caller lacks the {role.value} role required for this operation"
            )

    def may_approve(self) -> bool:
        return CallerRole.APPROVER in self.roles


class TokenDecoder(Protocol):
    """Cryptographic verification, injected.

    A protocol rather than a concrete dependency so claim policy can be tested exhaustively without
    a live Entra tenant or a JWKS endpoint — and so the production implementation is a thin adapter
    with nothing interesting to get wrong.
    """

    def decode(self, token: str) -> dict[str, object]:
        """Verify signature and expiry, returning claims. Raise on any failure."""
        ...


@dataclass
class TokenPolicy:
    """Claim requirements. Configuration, never conversation-adjustable.

    ``allowed_issuers`` is mutable by design (2026-08-25): the control-plane lifespan seeds it
    with the home tenant's issuers plus every *onboarded customer tenant's* issuers, and refreshes
    it as customers onboard. Issuers for tenants that were never onboarded are never added — the
    set is derived from the same tenant registry every other authorization surface reads.
    """

    expected_audience: str
    allowed_issuers: frozenset[str]
    require_role: bool = True

    def issuers_for_tenant(self, tenant_id: str) -> frozenset[str]:
        return frozenset(
            {
                f"https://login.microsoftonline.com/{tenant_id}/v2.0",
                f"https://sts.windows.net/{tenant_id}/",
            }
        )

    def add_tenant(self, tenant_id: str) -> None:
        self.allowed_issuers = self.allowed_issuers | self.issuers_for_tenant(tenant_id)


def refresh_cross_tenant_auth(
    register_tenant: Callable[[str], None],
    policy: TokenPolicy,
    onboarded_tenant_ids: Iterable[str],
) -> int:
    """Register every onboarded customer tenant for token routing and issuer trust.

    Idempotent. Called once at startup (with the registry's initial snapshot) and then
    periodically from the control-plane lifespan, so a customer who onboards after startup can
    authenticate without a pod restart. Returns how many tenants were first-seen here.
    """
    known = set(policy.allowed_issuers)
    newly = 0
    for tenant_id in onboarded_tenant_ids:
        marker = f"https://login.microsoftonline.com/{tenant_id}/v2.0"
        register_tenant(tenant_id)
        policy.add_tenant(tenant_id)
        if marker not in known:
            newly += 1
            known.add(marker)
    return newly


def _as_str(claims: dict[str, object], key: str) -> str | None:
    value = claims.get(key)
    return value if isinstance(value, str) and value else None


def _as_timestamp(claims: dict[str, object], key: str) -> datetime | None:
    value = claims.get(key)
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(float(value), tz=UTC)


def _as_str_set(claims: dict[str, object], key: str) -> frozenset[str]:
    value = claims.get(key)
    if not isinstance(value, list):
        return frozenset()
    return frozenset(item for item in value if isinstance(item, str) and item)


class TokenValidator:
    """Validates bearer tokens and produces an :class:`AuthenticatedCaller`.

    Order matters: signature and expiry are checked by the decoder before any claim is read, so a
    forged token never reaches claim policy.
    """

    def __init__(self, decoder: TokenDecoder, policy: TokenPolicy) -> None:
        self._decoder = decoder
        self._policy = policy

    def validate(
        self, authorization_header: str | None, *, now: datetime | None = None
    ) -> AuthenticatedCaller:
        """Validate an ``Authorization`` header value.

        Raises:
            AuthenticationError: On any failure. Never returns a partially trusted caller — there
                is no such thing.
        """
        token = self._extract_bearer(authorization_header)

        try:
            claims = self._decoder.decode(token)
        except Exception as exc:
            # Broad catch at an adapter boundary: any decoder failure — bad signature, unknown key,
            # malformed segment — is authentication failure. Chained so the cause survives in logs,
            # which are scrubbed before emission.
            raise AuthenticationError("token failed cryptographic validation") from exc

        self._check_audience(claims)
        self._check_issuer(claims)

        tenant_id = _as_str(claims, "tid")
        if tenant_id is None:
            raise AuthenticationError("token carries no tenant claim")

        object_id = _as_str(claims, "oid")
        if object_id is None:
            raise AuthenticationError("token carries no object identifier claim")

        expires_at = self._expiry(claims)
        moment = now or datetime.now(UTC)
        if expires_at <= moment:
            # The decoder normally rejects expired tokens; this is a second, independent check so a
            # misconfigured decoder cannot silently admit them.
            raise AuthenticationError("token has expired")

        roles = self._roles(claims)
        if self._policy.require_role and not roles:
            raise AuthenticationError("token carries no recognised application role")

        return AuthenticatedCaller(
            object_id=object_id,
            tenant_id=tenant_id,
            display_name=_as_str(claims, "name") or object_id,
            roles=roles,
            token_expires_at=expires_at,
            authentication_methods=_as_str_set(claims, "amr"),
            token_issued_at=_as_timestamp(claims, "iat"),
        )

    @staticmethod
    def _extract_bearer(header: str | None) -> str:
        if not header:
            raise AuthenticationError("no authorization header")
        parts = header.split(None, 1)
        if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1].strip():
            raise AuthenticationError("authorization header is not a bearer token")
        return parts[1].strip()

    def _check_audience(self, claims: dict[str, object]) -> None:
        audience = claims.get("aud")
        # Entra may present `aud` as a string or a list depending on the token type.
        accepted = (
            {audience}
            if isinstance(audience, str)
            else set(audience)
            if isinstance(audience, list)
            else set()
        )
        # Found live: a v2.0 access token acquired against this app's own `api://<guid>` scope
        # carries the bare client-ID GUID as `aud`, not the `api://<guid>` App ID URI the scope
        # itself was requested with — both are the same application, just two accepted spellings
        # of the same audience Entra uses depending on token path. Rejecting the GUID form failed
        # every real browser-acquired token while a hand-built value matching the config string
        # exactly would have passed, which is exactly backwards for what this check is for.
        expected = {self._policy.expected_audience}
        if self._policy.expected_audience.startswith("api://"):
            expected.add(self._policy.expected_audience.removeprefix("api://"))
        if expected.isdisjoint(accepted):
            raise AuthenticationError("token audience does not match this API")

    def _check_issuer(self, claims: dict[str, object]) -> None:
        issuer = _as_str(claims, "iss")
        if issuer is None or issuer not in self._policy.allowed_issuers:
            raise AuthenticationError("token issuer is not trusted")

    @staticmethod
    def _expiry(claims: dict[str, object]) -> datetime:
        exp = claims.get("exp")
        if not isinstance(exp, int | float):
            raise AuthenticationError("token carries no usable expiry claim")
        return datetime.fromtimestamp(float(exp), tz=UTC)

    @staticmethod
    def _roles(claims: dict[str, object]) -> frozenset[CallerRole]:
        raw = claims.get("roles")
        if not isinstance(raw, list):
            return frozenset()
        recognised: set[CallerRole] = set()
        for item in raw:
            if not isinstance(item, str):
                continue
            try:
                recognised.add(CallerRole(item))
            except ValueError:
                # An unrecognised role is ignored, not an error. Entra tokens routinely carry roles
                # for other applications, and rejecting them would couple this API to unrelated
                # role catalogues.
                continue
        return frozenset(recognised)
