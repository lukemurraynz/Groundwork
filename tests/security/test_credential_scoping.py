"""T021 / FR-032 — a per-tenant credential must not be usable for another tenant.

This is the isolation guarantee SC-011 tests at the system level ("zero cross-tenant data or
credential access under concurrent multi-tenant load"). Here it is tested at the unit that actually
enforces it: the only way to get a token out of a :class:`TenantScopedCredential` is to name the
tenant it was built for, and any other name is refused.

A fake credential stands in for ``DefaultAzureCredential`` so this proves the *scoping logic*
without needing a live workload identity federation — the real credential resolution is Azure's
concern, not ours to re-test.
"""

from __future__ import annotations

import pytest

from groundwork_shared.identity.credentials import (
    CrossTenantAccessError,
    TenantScopedCredentialFactory,
)

pytestmark = pytest.mark.security

TENANT_A = "11111111-1111-1111-1111-111111111111"
TENANT_B = "22222222-2222-2222-2222-222222222222"


class FakeCredential:
    """Stands in for a real TokenCredential. Never touches Entra."""

    def get_token(self, *scopes: str, **kwargs: object) -> object:
        return object()


def test_credential_authorises_its_own_tenant() -> None:
    factory = TenantScopedCredentialFactory(credential=FakeCredential())
    scoped = factory.scoped_to(TENANT_A)

    assert scoped.for_tenant(TENANT_A) is not None


def test_credential_refuses_a_different_tenant() -> None:
    """FR-032 — the isolation SC-011 tests at the system level, enforced here at the unit."""
    factory = TenantScopedCredentialFactory(credential=FakeCredential())
    scoped = factory.scoped_to(TENANT_A)

    with pytest.raises(CrossTenantAccessError) as excinfo:
        scoped.for_tenant(TENANT_B)

    assert excinfo.value.expected_tenant == TENANT_A
    assert excinfo.value.requested_tenant == TENANT_B


def test_two_tenants_get_structurally_distinct_scoped_credentials() -> None:
    """Two concurrent deployments for different tenants must not be able to cross-authorise."""
    factory = TenantScopedCredentialFactory(credential=FakeCredential())

    scoped_a = factory.scoped_to(TENANT_A)
    scoped_b = factory.scoped_to(TENANT_B)

    scoped_a.for_tenant(TENANT_A)
    scoped_b.for_tenant(TENANT_B)
    with pytest.raises(CrossTenantAccessError):
        scoped_a.for_tenant(TENANT_B)
    with pytest.raises(CrossTenantAccessError):
        scoped_b.for_tenant(TENANT_A)


def test_empty_tenant_id_is_rejected_at_scoping_time() -> None:
    """Fail at construction, not when the credential is later misused."""
    factory = TenantScopedCredentialFactory(credential=FakeCredential())

    with pytest.raises(ValueError, match="non-empty"):
        factory.scoped_to("")


def test_scoped_credential_is_immutable() -> None:
    factory = TenantScopedCredentialFactory(credential=FakeCredential())
    scoped = factory.scoped_to(TENANT_A)

    with pytest.raises(AttributeError):
        scoped.tenant_id = TENANT_B  # type: ignore[misc]
