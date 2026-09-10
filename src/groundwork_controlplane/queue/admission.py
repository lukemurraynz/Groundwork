"""Per-tenant concurrency admission (T068; FR-045a).

Queueing, not rejecting: a tenant already at its concurrency cap does not get an error for a new
deployment request. The request is accepted and queued, with its position reported — FR-045a's own
requirement is that "the caller is told it is queued rather than left to assume it is running."

Split into a pure decision function and a repository-querying wrapper, the same reasoning as every
readiness check in ``validation/checks/*.py``: the query is Cosmos's job to get right, the counting
rule is what is worth testing directly and exhaustively.
"""

from __future__ import annotations

from groundwork_contracts.deployment import DeploymentStatus
from groundwork_orchestrator.state.repositories import DeploymentRepository


def compute_admission(*, active_count: int, queued_count: int, concurrency_cap: int) -> int | None:
    """Return the new deployment's ``queue_position``, or ``None`` if admitted without queueing.

    Args:
        active_count: The tenant's current count of ``QUEUED`` + ``EXECUTING`` deployments,
            *before* this new one is added.
        queued_count: The subset of ``active_count`` that is specifically ``QUEUED`` — queue
            position ranks a new arrival among others waiting, not among ones already executing.
        concurrency_cap: :class:`~groundwork_contracts.tenant.CustomerTenant.concurrency_cap`.

    Returns:
        ``None`` if ``active_count`` is below the cap (not queued — a separate question, the
        per-subscription lease, still gates whether it can actually start executing). Otherwise a
        1-indexed position: the first deployment queued behind a full cap is position 1.
    """
    if active_count < concurrency_cap:
        return None
    return queued_count + 1


async def evaluate_admission(
    deployment_repository: DeploymentRepository, tenant_id: str, *, concurrency_cap: int
) -> int | None:
    """Query the tenant's active deployments and apply :func:`compute_admission` to them."""
    active_count = 0
    queued_count = 0
    async for deployment in deployment_repository.query(
        tenant_id,
        "SELECT * FROM c WHERE c.status IN ('queued', 'executing')",
    ):
        active_count += 1
        if deployment.status is DeploymentStatus.QUEUED:
            queued_count += 1

    return compute_admission(
        active_count=active_count, queued_count=queued_count, concurrency_cap=concurrency_cap
    )
