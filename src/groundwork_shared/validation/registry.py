"""Shared readiness registry and context builder.

The control plane uses this for plan-time validation; the orchestrator uses the exact same registry
for periodic drift re-evaluation.
"""

from __future__ import annotations

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.blueprint import PlatformBlueprint
from groundwork_shared.config.settings import ReadinessSettings
from groundwork_shared.validation.checks import (
    devops,
    fabric,
    identity,
    naming,
    network,
    policy,
    quota,
    tenant,
)
from groundwork_shared.validation.checks.network import DEFAULT_VNET_ADDRESS_SPACE
from groundwork_shared.validation.engine import CheckFunction, ValidationContext

CHECKS: dict[str, CheckFunction] = {
    tenant.ASSERTION_ID: tenant.subscription_reachable,
    identity.DEPLOYMENT_IDENTITY_NOT_OWNER_ASSERTION_ID: identity.deployment_identity_not_owner,
    identity.NO_BLANKET_DENY_ASSERTION_ID: identity.no_blanket_deny_assignment,
    fabric.ASSERTION_ID: fabric.fabric_service_principal_api_enabled,
    naming.ASSERTION_ID: naming.resource_group_not_in_use,
    network.ASSERTION_ID: network.vnet_address_space_available,
    quota.ASSERTION_ID: quota.resource_providers_registered,
    policy.ASSERTION_ID: policy.no_denying_resource_type_policy,
    devops.ASSERTION_ID: devops.organization_reachable,
}


def build_validation_context(
    *,
    tenant_id: str,
    subscription_id: str,
    region: str,
    credential: AsyncTokenCredential,
    readiness: ReadinessSettings,
    tenant_devops_organization_url: str | None = None,
    blueprint: PlatformBlueprint | None = None,
) -> ValidationContext:
    return ValidationContext(
        tenant_id=tenant_id,
        subscription_id=subscription_id,
        region=region,
        vnet_address_space=DEFAULT_VNET_ADDRESS_SPACE,
        deployment_identity_object_id=readiness.orchestrator_principal_id,
        credential=credential,
        devops_organization_url=(
            tenant_devops_organization_url or readiness.devops_organization_url
        ),
        required_resource_providers=_required_resource_providers(blueprint),
    )


def _required_resource_providers(blueprint: PlatformBlueprint | None) -> tuple[str, ...] | None:
    if blueprint is None:
        return None

    module_provider_map = {
        "avm/res/resources/resource-group": "Microsoft.Resources",
        "avm/res/network/virtual-network": "Microsoft.Network",
        "avm/res/key-vault/vault": "Microsoft.KeyVault",
        "avm/res/operational-insights/workspace": "Microsoft.OperationalInsights",
        "avm/res/insights/component": "Microsoft.Insights",
        "avm/res/managed-identity/user-assigned-identity": "Microsoft.ManagedIdentity",
    }
    providers = {
        provider
        for artefact in blueprint.iac_artefacts
        for module, provider in module_provider_map.items()
        if artefact.module == module
    }
    if any(stage.name == "fabric" for stage in blueprint.stages):
        providers.add("Microsoft.Fabric")
    return tuple(sorted(providers))
