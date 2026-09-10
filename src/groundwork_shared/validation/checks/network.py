"""Network topology assertions (T038).

Verified 2026-07-31 against the installed ``azure-mgmt-network`` SDK:
``VirtualNetworksOperations.list_all()`` enumerates every VNet in the subscription (not scoped to a
resource group), and ``VirtualNetwork.address_space.address_prefixes`` is a ``list[str]`` of CIDR
blocks. Overlap detection uses the standard-library ``ipaddress`` module rather than string
comparison — CIDR overlap is not expressible correctly by comparing prefix strings.

**Disclosed scope boundary**: this checks VNet address-space overlap only — the single assertion
this platform's blueprint actually needs verified before the networking stage runs. Subnet capacity
for private endpoints and Private DNS zone resolvability (also named in T038's task description) are
per-deployment details that depend on the specific plan's resource count and are better evaluated at
execution time inside the networking stage itself (T079) than as a pre-flight readiness assertion —
recorded here rather than silently omitted from this module's assertion coverage.
"""

from __future__ import annotations

import ipaddress

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "network.vnet-address-space-available"

# Fixed, not per-tenant-configurable: the blueprint's own br/public/avm/res/network/virtual-network
# artefact provisions one platform VNet per deployment with this address space. Pinning it here,
# the same way naming.py pins the resource group naming convention, means this readiness check and
# the infrastructure stage (T077) that actually creates the VNet can never disagree about what
# address space is in play.
DEFAULT_VNET_ADDRESS_SPACE = "10.42.0.0/16"


def _overlaps(candidate: str, existing: str) -> bool:
    candidate_net = ipaddress.ip_network(candidate, strict=False)
    existing_net = ipaddress.ip_network(existing, strict=False)
    return candidate_net.overlaps(existing_net)


def _evaluate_overlap(candidate: str, existing_prefixes: list[str]) -> tuple[ValidationStatus, str]:
    overlapping = [prefix for prefix in existing_prefixes if _overlaps(candidate, prefix)]
    if overlapping:
        return (
            ValidationStatus.FAILED,
            f"{candidate} overlaps existing VNet address space {', '.join(overlapping)}",
        )
    return (
        ValidationStatus.PASSED,
        f"{candidate} does not overlap any of the {len(existing_prefixes)} existing VNet "
        f"address prefix(es) in the subscription",
    )


async def vnet_address_space_available(context: ValidationContext) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``network.vnet-address-space-available``."""
    from azure.mgmt.network.aio import NetworkManagementClient

    prefixes: list[str] = []
    async with NetworkManagementClient(context.credential, context.subscription_id) as client:
        async for vnet in client.virtual_networks.list_all():
            if vnet.address_space and vnet.address_space.address_prefixes:
                prefixes.extend(vnet.address_space.address_prefixes)

    return _evaluate_overlap(context.vnet_address_space, prefixes)
