"""Real Entra ID token cryptographic verification.

Implements the ``TokenDecoder`` protocol from ``auth.py`` using PyJWT's ``PyJWKClient``, which
fetches and caches the tenant's actual signing keys from its JWKS endpoint and verifies both the
signature and standard registered claims (expiry, not-before). This is the "delegate the
cryptography" half of auth.py's design — claim *policy* (audience, issuer, tenant/object
extraction) stays in ``TokenValidator``; this class only proves the token is authentic.

"Do not implement custom cryptography" is a standing rule. Nothing here reimplements JWT
verification; it configures a maintained library against Microsoft's own published keys.
"""

from __future__ import annotations

import jwt
from jwt import PyJWKClient
from jwt import decode as jwt_decode


class EntraTokenDecoder:
    """Verifies a token against one Entra tenant's real signing keys.

    One instance per tenant the control plane trusts tokens from. The JWKS client caches keys and
    handles Microsoft's periodic key rotation, so this class does not need to.
    """

    def __init__(self, tenant_id: str) -> None:
        jwks_uri = f"https://login.microsoftonline.com/{tenant_id}/discovery/v2.0/keys"
        self._jwks_client = PyJWKClient(jwks_uri, cache_keys=True)

    def decode(self, token: str) -> dict[str, object]:
        """Verify signature and standard claims, returning the raw claim set.

        Audience is deliberately NOT checked here — ``TokenValidator._check_audience`` owns that,
        because it needs to accept Entra's list-form ``aud`` claim, which PyJWT's built-in audience
        check does not handle the same way. Signature, issuer format, and expiry are still fully
        verified by this call; only the audience assertion is deferred to the caller.
        """
        signing_key = self._jwks_client.get_signing_key_from_jwt(token)
        return jwt_decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )


class MultiTenantTokenDecoder:
    """Routes a token to the right :class:`EntraTokenDecoder` based on its ``tid`` claim.

    Added 2026-08-25 for cross-tenant customers (FR-006): an onboarded customer's admin signs in
    against *their* Entra tenant, so their tokens carry their tenant's signing keys and issuer.
    Trust boundaries are unchanged — the ``tid`` is read from an UNVERIFIED peek purely to select
    which verifier's JWKS to use, and every token is then fully signature-verified by that
    selected decoder before any claim influences an authorization decision. A ``tid`` outside the
    provisioned set falls back to the home decoder, whose verification fails exactly as it would
    have before this class existed.
    """

    def __init__(self, *, home_tenant_id: str) -> None:
        self._home_tenant_id = home_tenant_id
        self._decoders: dict[str, EntraTokenDecoder] = {
            home_tenant_id: EntraTokenDecoder(home_tenant_id)
        }

    def register_tenant(self, tenant_id: str) -> None:
        """Provision (or re-provision) a customer tenant's decoder. Idempotent."""
        self._decoders.setdefault(tenant_id, EntraTokenDecoder(tenant_id))

    def known_tenants(self) -> frozenset[str]:
        return frozenset(self._decoders)

    def decode(self, token: str) -> dict[str, object]:
        try:
            unverified = jwt.decode(token, options={"verify_signature": False})
        except Exception as exc:
            raise ValueError("token could not be parsed") from exc
        tenant_id = unverified.get("tid")
        tenant_id = tenant_id if isinstance(tenant_id, str) and tenant_id else self._home_tenant_id
        decoder = self._decoders.get(tenant_id)
        if decoder is None:
            # Unknown tenant: verify against home keys so the failure is cryptographic and
            # indistinguishable from any other untrusted token.
            decoder = self._decoders[self._home_tenant_id]
        return decoder.decode(token)
