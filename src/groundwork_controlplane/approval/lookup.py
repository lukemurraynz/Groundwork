"""Finding an :class:`Approval` or :class:`PendingApproval` by a field other than its Cosmos id.

Both types share one Cosmos container (see
``groundwork_orchestrator.state.repositories.pending_approval_repository``'s docstring). A plain
``TenantScopedRepository.read`` by id would risk calling ``Approval.model_validate`` on a document
that is actually still a ``PendingApproval`` — which does not just fail to match, it can raise:
``Approval``'s own ``_second_approver_present_and_distinct`` validator rejects a ``second_approval``
of ``None`` whenever the threshold requires one, and a genuinely-still-pending document always
meets that condition. Both lookups in this module route around that by filtering on
``second_approval``'s presence in the query itself, so each typed repository only ever receives
documents shaped the way it expects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from groundwork_contracts.approval import Approval, PendingApproval

if TYPE_CHECKING:
    from groundwork_orchestrator.state.repositories import (
        ApprovalRepository,
        PendingApprovalRepository,
    )


_PENDING_BY_PLAN_HASH = (
    "SELECT * FROM c WHERE c.plan_hash = @value AND NOT IS_DEFINED(c.second_approval)"
)
_COMPLETE_BY_PLAN_HASH = (
    "SELECT * FROM c WHERE c.plan_hash = @value AND IS_DEFINED(c.second_approval)"
)
_PENDING_BY_ID = "SELECT * FROM c WHERE c.id = @value AND NOT IS_DEFINED(c.second_approval)"
_COMPLETE_BY_ID = "SELECT * FROM c WHERE c.id = @value AND IS_DEFINED(c.second_approval)"


async def _find_by(
    *,
    pending_repository: PendingApprovalRepository,
    approval_repository: ApprovalRepository,
    tenant_id: str,
    value: str,
    pending_query: str,
    complete_query: str,
) -> Approval | PendingApproval | None:
    parameters = [{"name": "@value", "value": value}]

    async for pending in pending_repository.query(tenant_id, pending_query, parameters):
        return pending

    async for complete in approval_repository.query(tenant_id, complete_query, parameters):
        return complete

    return None


async def find_approval_by_plan_hash(
    *,
    pending_repository: PendingApprovalRepository,
    approval_repository: ApprovalRepository,
    tenant_id: str,
    plan_hash: str,
) -> Approval | PendingApproval | None:
    """Whatever exists for this plan — used when creating or checking a plan's own approval."""
    return await _find_by(
        pending_repository=pending_repository,
        approval_repository=approval_repository,
        tenant_id=tenant_id,
        value=plan_hash,
        pending_query=_PENDING_BY_PLAN_HASH,
        complete_query=_COMPLETE_BY_PLAN_HASH,
    )


async def find_approval_by_id(
    *,
    pending_repository: PendingApprovalRepository,
    approval_repository: ApprovalRepository,
    tenant_id: str,
    approval_id: str,
) -> Approval | PendingApproval | None:
    """A specific approval by its own id — used when a caller names one directly, e.g.
    ``POST /deployments``'s ``approvalId``."""
    return await _find_by(
        pending_repository=pending_repository,
        approval_repository=approval_repository,
        tenant_id=tenant_id,
        value=approval_id,
        pending_query=_PENDING_BY_ID,
        complete_query=_COMPLETE_BY_ID,
    )
