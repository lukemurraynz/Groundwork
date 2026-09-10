"""Naming convention and resource-conflict assertions (T041).

Verified 2026-07-31 against the installed ``azure-mgmt-resource`` SDK:
``ResourceGroupsOperations.check_existence(resource_group_name)`` returns a plain ``bool`` — no
exception on "not found", which is the normal, expected outcome for a fresh deployment target.

Split into Azure-calling glue and a pure decision function — see ``tenant.py``'s module docstring
for why.
"""

from __future__ import annotations

from azure.mgmt.resource.resources.aio import ResourceManagementClient

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

ASSERTION_ID = "naming.resource-group-not-in-use"


def deployment_resource_group_name(subscription_id: str) -> str:
    """Deterministic resource group name for a deployment target.

    Matches this platform's own ``infra/main.bicep`` naming convention, applied to the customer
    tenant. Deriving it the same way here and in the infrastructure stage (T077) means this check
    and that stage can never disagree about what name is in play.
    """
    return f"rg-groundwork-{subscription_id[:8]}"


def _evaluate_existence(resource_group_name: str, exists: bool) -> tuple[ValidationStatus, str]:
    if exists:
        return (
            ValidationStatus.FAILED,
            f"resource group {resource_group_name!r} already exists",
        )
    return ValidationStatus.PASSED, f"resource group {resource_group_name!r} is available"


async def resource_group_not_in_use(context: ValidationContext) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``naming.resource-group-not-in-use``."""
    resource_group_name = deployment_resource_group_name(context.subscription_id)

    async with ResourceManagementClient(context.credential, context.subscription_id) as client:
        exists = await client.resource_groups.check_existence(resource_group_name)

    return _evaluate_existence(resource_group_name, exists)
