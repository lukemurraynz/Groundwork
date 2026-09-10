"""Resource-provider registration assertions (T039).

Verified 2026-07-31 against the installed ``azure-mgmt-resource`` SDK:
``ProvidersOperations.get(namespace)`` returns a ``Provider`` whose ``registration_state`` is a
plain ``str`` (``"Registered"`` when registered), matching the same fact this session already
confirmed live via ``az provider show --query registrationState`` while building the Bicep
infrastructure.
"""

from __future__ import annotations

from azure.mgmt.resource.resources.aio import ResourceManagementClient

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "quota.resource-provider-registered"

REGISTERED = "Registered"

# Default provider set, matching the long-shipped standard-production-fabric blueprint. Newer
# blueprint-aware callers thread an explicit per-blueprint list via ValidationContext.
REQUIRED_PROVIDERS = (
    "Microsoft.Network",
    "Microsoft.KeyVault",
    "Microsoft.OperationalInsights",
    "Microsoft.Insights",
    "Microsoft.ManagedIdentity",
    "Microsoft.Fabric",
)


def _evaluate_registrations(states: dict[str, str]) -> tuple[ValidationStatus, str]:
    unregistered = sorted(namespace for namespace, state in states.items() if state != REGISTERED)
    if unregistered:
        return (
            ValidationStatus.FAILED,
            f"resource provider(s) not registered: {', '.join(unregistered)}",
        )
    return (
        ValidationStatus.PASSED,
        f"all {len(states)} required resource providers are registered",
    )


async def resource_providers_registered(
    context: ValidationContext,
) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``quota.resource-provider-registered``."""
    states: dict[str, str] = {}
    required = context.required_resource_providers or REQUIRED_PROVIDERS
    async with ResourceManagementClient(context.credential, context.subscription_id) as client:
        for namespace in required:
            provider = await client.providers.get(namespace)
            states[namespace] = provider.registration_state or ""

    return _evaluate_registrations(states)
