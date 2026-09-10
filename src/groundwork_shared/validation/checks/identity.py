"""Identity, permission, and deny-assignment assertions (T037).

Covers two assertions from two different Landing Zone design areas — identity-and-access-management
and security — because both are, at bottom, "can the identity stage actually grant what the
blueprint declares" questions answered by the same Authorization management API.

Verified 2026-08-26 against Microsoft Learn and the installed ``azure-mgmt-authorization`` SDK:
``RoleAssignmentsOperations`` and ``DenyAssignmentsOperations`` expose ``list_for_scope(...)`` on
the async client, not ``list_for_subscription(...)``. Subscription-scope queries therefore pass the
explicit scope string ``/subscriptions/<id>``. The Owner role definition GUID
(``8e3af657-a8ff-443c-a75c-2fe8c4bcb635``) was confirmed live with
``az role definition list --name Owner``.
"""

from __future__ import annotations

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.engine import ValidationContext

DEPLOYMENT_IDENTITY_NOT_OWNER_ASSERTION_ID = "identity.deployment-identity-not-owner"
NO_BLANKET_DENY_ASSERTION_ID = "security.no-blanket-deny-assignment"

OWNER_ROLE_DEFINITION_ID_SUFFIX = "/8e3af657-a8ff-443c-a75c-2fe8c4bcb635"
_SUBSCRIPTION_SCOPE_PREFIX = "/subscriptions/"


def _evaluate_not_owner(has_owner: bool, principal_id: str) -> tuple[ValidationStatus, str]:
    if has_owner:
        return (
            ValidationStatus.FAILED,
            f"deployment identity {principal_id} already holds Owner at subscription scope",
        )
    return (
        ValidationStatus.PASSED,
        f"deployment identity {principal_id} holds no standing Owner assignment",
    )


async def deployment_identity_not_owner(
    context: ValidationContext,
) -> tuple[ValidationStatus, str]:
    """FR-014 / FR-009 assertion ``identity.deployment-identity-not-owner``."""
    from azure.mgmt.authorization.aio import AuthorizationManagementClient

    subscription_scope = f"{_SUBSCRIPTION_SCOPE_PREFIX}{context.subscription_id}"
    async with AuthorizationManagementClient(context.credential, context.subscription_id) as client:
        has_owner = False
        async for assignment in client.role_assignments.list_for_scope(
            subscription_scope,
            filter=f"principalId eq '{context.deployment_identity_object_id}'",
        ):
            role_definition_id = (
                getattr(getattr(assignment, "properties", None), "role_definition_id", "") or ""
            )
            if role_definition_id.endswith(OWNER_ROLE_DEFINITION_ID_SUFFIX):
                has_owner = True
                break

    return _evaluate_not_owner(has_owner, context.deployment_identity_object_id)


def _evaluate_deny_assignments(names: list[str]) -> tuple[ValidationStatus, str]:
    if names:
        return (
            ValidationStatus.FAILED,
            f"{len(names)} deny assignment(s) present at subscription scope: {', '.join(names)}",
        )
    return (
        ValidationStatus.PASSED,
        "no deny assignment blocks role assignment at subscription scope",
    )


async def no_blanket_deny_assignment(context: ValidationContext) -> tuple[ValidationStatus, str]:
    """FR-014 assertion ``security.no-blanket-deny-assignment``.

    A deny assignment blocking role assignment itself would prevent the identity stage from
    granting the blueprint's declared least-privilege roles — this is a readiness gate, not a
    general audit of every deny assignment's scope or target actions.
    """
    from azure.mgmt.authorization.aio import AuthorizationManagementClient

    names: list[str] = []
    subscription_scope = f"{_SUBSCRIPTION_SCOPE_PREFIX}{context.subscription_id}"
    async with AuthorizationManagementClient(context.credential, context.subscription_id) as client:
        async for assignment in client.deny_assignments.list_for_scope(subscription_scope):
            names.append(assignment.deny_assignment_name or assignment.name or "unnamed")

    return _evaluate_deny_assignments(names)
