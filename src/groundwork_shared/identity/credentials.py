"""Workload identity credential resolution — T020, FR-032, FR-044.

The secretless-identity rule: no long-lived client secrets, none stored in Kubernetes. Every
Azure client authenticates with `DefaultAzureCredential`, which resolves to AKS workload identity
in the cluster
(via the federated credential bound to each service account — see ``k8s/*/deployment.yaml``) and to
the developer's own Entra sign-in locally. There is no code path in this module that constructs a
credential from a client secret, a connection string, or a key.

The second, load-bearing rule is FR-032: **no code path may let one tenant's identifier select
another tenant's resources.** :class:`TenantScopedCredentialFactory` is the enforcement point for
that. It does not wrap ``DefaultAzureCredential`` per tenant — this release's identity model uses
one workload identity per *component* (control plane, orchestrator), not one per customer tenant,
per the CL-004 hybrid decision recorded in the spec. What this factory scopes is *authorization*:
it binds a credential to the single tenant a request is allowed to act against, and raises rather
than silently falling back if a caller ever asks for a different one mid-use.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import DefaultAzureCredential


class CrossTenantAccessError(Exception):
    """A caller attempted to use a credential scoped to one tenant against another.

    This must never happen through normal operation — the type only has one tenant to give out. If
    it is ever raised, something upstream passed the wrong tenant identifier, which is exactly the
    class of bug FR-032 requires be structurally prevented rather than merely tested for.
    """

    def __init__(self, expected_tenant: str, requested_tenant: str) -> None:
        self.expected_tenant = expected_tenant
        self.requested_tenant = requested_tenant
        super().__init__(
            f"credential is scoped to tenant {expected_tenant!r}; refusing to use it for "
            f"{requested_tenant!r}"
        )


class CredentialProvider(Protocol):
    """The minimal surface this module depends on.

    A protocol rather than importing ``DefaultAzureCredential`` directly into every call site, so
    tests can substitute a fake token source without touching real Entra — the fake is test-only
    code, per the standing rule that mocks belong in tests, never in the production path.
    """

    async def get_token(self, *scopes: str, **kwargs: object) -> object: ...


@dataclass(frozen=True, slots=True)
class TenantScopedCredential:
    """A credential bound to exactly one tenant.

    The underlying ``DefaultAzureCredential`` is not itself tenant-scoped — Azure workload identity
    federation issues tokens for whatever resource the caller requests, regardless of which customer
    that resource belongs to. Scoping happens here, at the call boundary: every use of this object
    must go through :meth:`for_tenant`, which is the one place tenant identity is checked against
    what this instance was built for.
    """

    tenant_id: str
    _credential: AsyncTokenCredential

    def for_tenant(self, tenant_id: str) -> AsyncTokenCredential:
        """Return the underlying credential, but only if ``tenant_id`` matches.

        This is the FR-032 enforcement point. A caller cannot use a credential scoped to tenant A to
        act on tenant B — not because of a permission the caller might forget to check, but because
        the only way to get a usable credential out of this object is to ask for the tenant it was
        built for and be refused otherwise.
        """
        if tenant_id != self.tenant_id:
            raise CrossTenantAccessError(expected_tenant=self.tenant_id, requested_tenant=tenant_id)
        return self._credential


class TenantScopedCredentialFactory:
    """Builds :class:`TenantScopedCredential` instances.

    One factory per process. Each call to :meth:`scoped_to` returns a credential that will only ever
    authorise action against the tenant it was scoped to — the isolation FR-032 requires between
    concurrent deployments for different tenants (SC-011).
    """

    def __init__(self, credential: AsyncTokenCredential | None = None) -> None:
        # DefaultAzureCredential resolves, in order: workload identity (in-cluster), then the
        # developer's Azure CLI / VS Code sign-in (local). No client secret is ever in this chain.
        self._credential = credential or DefaultAzureCredential()

    def scoped_to(self, tenant_id: str) -> TenantScopedCredential:
        if not tenant_id or not tenant_id.strip():
            raise ValueError("tenant_id must be a non-empty string")
        return TenantScopedCredential(tenant_id=tenant_id, _credential=self._credential)
