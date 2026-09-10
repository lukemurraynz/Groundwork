"""Fabric service-principal API access assertion — the ninth readiness check.

Verified 2026-08-21 against live Microsoft Learn documentation:

- ``learn.microsoft.com/rest/api/fabric/articles/identity-support``: service principals and
  managed identities cannot call any Fabric public API unless the tenant setting "Service
  principals can call Fabric public APIs" (formerly titled "Service principals can use Fabric
  APIs") is enabled by a Fabric administrator — and when that setting is scoped to specific
  security groups, only principals inside those groups are permitted.
- ``learn.microsoft.com/fabric/admin/service-admin-portal-developer``: a second, separate
  Developer-settings switch ("Service principals can create workspaces, connections, and
  deployment pipelines") gates workspace creation specifically; the fabric stage creates a
  workspace, so both switches must permit the deployment identity.

This check *probes* the Fabric public API rather than reading the tenant settings via the admin
API (``GET /v1/admin/tenantsettings``, which does support service principals). Probing was chosen
deliberately: the admin API would additionally require the Tenant.Read.All application permission
consented in the customer tenant and is itself gated by a further tenant setting for
service-principal callers — new prerequisites this platform does not otherwise need — whereas the
probe verifies the actual capability end to end using the same credential and scope the fabric
stage itself uses, and handles security-group scoping without any group-membership lookup.

Disclosed residual gap: a passing probe proves the base SP-API gate accepts this identity; it
cannot prove the separate workspace-creation switch allows it (creating a workspace to test that
would not be read-only). Both switches are named in the assertion's remediation text so an
operator enabling access enables both.

Auth scope ``https://api.fabric.microsoft.com/.default`` matches
``groundwork_orchestrator.stages.fabric.FABRIC_SCOPE`` exactly; the constant is repeated here
because the control plane must never import from the orchestrator package.
"""

from __future__ import annotations

import httpx

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "fabric.service-principal-api-enabled"

FABRIC_API_ENDPOINT = "https://api.fabric.microsoft.com/v1"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"


def _evaluate_response(status_code: int) -> tuple[ValidationStatus, str]:
    if status_code == 200:
        return (
            ValidationStatus.PASSED,
            "deployment identity successfully authenticated to the Fabric public API",
        )
    if status_code in (401, 403):
        return (
            ValidationStatus.FAILED,
            f"Fabric public API responded {status_code}; the 'Service principals can call "
            f"Fabric public APIs' tenant setting is likely disabled, or scoped to security "
            f"groups that exclude the deployment identity",
        )
    return (
        ValidationStatus.FAILED,
        f"Fabric public API responded with unexpected status {status_code}",
    )


async def fabric_service_principal_api_enabled(
    context: ValidationContext,
) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``fabric.service-principal-api-enabled``.

    A real call to the Fabric public API with the deployment identity's own token — the same
    credential shape every later fabric-stage call uses. If the identity cannot authenticate at
    all, the fabric stage would halt mid-deployment; failing here surfaces it at plan time
    instead. Transport failures raise and are reported as UNREACHABLE by the engine (FR-015).
    """
    token = await context.credential.get_token(FABRIC_SCOPE)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.get(
            f"{FABRIC_API_ENDPOINT}/capacities",
            headers={"Authorization": f"Bearer {token.token}"},
        )

    return _evaluate_response(response.status_code)
