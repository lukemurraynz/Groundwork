"""Azure DevOps reachability assertions (T042).

Verified 2026-07-31: ``499b84ac-1321-427f-aa17-267ca6975798`` is Microsoft's documented well-known
Entra ID resource ID for Azure DevOps (learn.microsoft.com/azure/devops/cli/entra-tokens) — the
same mechanism ``az account get-access-token --resource 499b84ac-...`` uses. No PAT, no service
connection secret: the secretless-identity rule holds here the same as everywhere else in
this codebase, using the tenant-scoped workload identity credential already on
:class:`ValidationContext` rather than a token acquired any other way.

Uses ``httpx`` directly rather than a dedicated SDK: there is no official Microsoft Python package
for the Azure DevOps REST API with async support comparable to the ``azure-mgmt-*`` clients used
elsewhere in this module — the REST API itself is the documented integration surface.
"""

from __future__ import annotations

import httpx
from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "devops.organization-reachable"

AZURE_DEVOPS_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
CONNECTION_DATA_API_VERSION = "7.1-preview"
"""Live-verified 2026-08-24 (``groundwork_orchestrator.stages.identity``): plain '7.1' 400s
("under preview... supply -preview") — this Location-service endpoint has never shipped a stable
api-version. Found drifted back to the wrong value here, a separate module defining the same
constant, on 2026-09-07 during a live deployment: the fix landed in one copy and never propagated
to this one, so every caller of this module's ``probe_organization_status`` (readiness checks,
conversation-time engagement-detail persistence) got a 400 on every real organization URL."""


def _evaluate_response(organization_url: str, status_code: int) -> tuple[ValidationStatus, str]:
    if status_code == 200:
        return ValidationStatus.PASSED, f"{organization_url} is reachable"
    if status_code in (401, 403):
        return (
            ValidationStatus.FAILED,
            f"{organization_url} responded {status_code}; the deployment identity has not been "
            f"added as an organization user",
        )
    if status_code == 404:
        return ValidationStatus.FAILED, f"{organization_url} does not exist or is not accessible"
    return (
        ValidationStatus.FAILED,
        f"{organization_url} responded with unexpected status {status_code}",
    )


async def probe_organization_status(
    organization_url: str,
    credential: AsyncTokenCredential,
    *,
    http_client: httpx.AsyncClient | None = None,
) -> int:
    """One authenticated ``GET {org}/_apis/connectionData`` — the raw status code.

    Shared by the readiness check above and the conversation-time engagement-data persistence
    path (``api/voice.py``), which verifies a customer-supplied organization URL *exists* before
    recording it on the tenant. The status semantics (200 reachable / 401,403 exists but the
    identity is not an org user / 404 no such org) are the readiness check's own, already
    verified against the live API — one probe, one interpretation, two callers.
    """
    token = await credential.get_token(f"{AZURE_DEVOPS_RESOURCE_ID}/.default")
    url = (
        f"{organization_url.rstrip('/')}/_apis/connectionData"
        f"?api-version={CONNECTION_DATA_API_VERSION}"
    )
    owns_client = http_client is None
    client = http_client or httpx.AsyncClient(timeout=10.0)
    try:
        response = await client.get(url, headers={"Authorization": f"Bearer {token.token}"})
    finally:
        if owns_client:
            await client.aclose()
    return response.status_code


async def organization_reachable(context: ValidationContext) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``devops.organization-reachable``.

    An absent ``devops_organization_url`` on the context is a configuration gap, not a check that
    could not run against a real target — it raises, and the engine reports it as UNREACHABLE
    (FR-015), which is the correct classification: nothing was actually asked of Azure DevOps.
    """
    if not context.devops_organization_url:
        raise ValueError("no Azure DevOps organization URL configured for this tenant")

    status_code = await probe_organization_status(
        context.devops_organization_url, context.credential
    )
    return _evaluate_response(context.devops_organization_url, status_code)
