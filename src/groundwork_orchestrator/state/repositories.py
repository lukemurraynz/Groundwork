"""Plan, approval, and deployment repositories (T024).

Thin, named factories rather than exposing :class:`TenantScopedRepository` directly with a type
parameter at every call site — a call reading ``plan_repository(store)`` states its intent, where
``store.repository("plans", model_cls=SealedDeploymentPlan, id_field=...)`` repeated at each site
would leave the container name and id field as details a reader has to re-verify every time.

Each repository's ``id_field`` is chosen from what the model actually has, not invented:

- Plans have no dedicated GUID id in :class:`~groundwork_contracts.plan.SealedDeploymentPlan` — only
  ``plan_hash``, which is already the content-derived identity FR-012 and FR-020 bind approval to.
  Using it as the Cosmos ``id`` costs nothing extra and needs no new field on an already-tested,
  already-sealed contract type.
- Approvals and deployments each have their own GUID (``approval_id``, ``deployment_id``); each uses
  it.
"""

from __future__ import annotations

from groundwork_contracts.approval import Approval, PendingApproval
from groundwork_contracts.audit import DeploymentReport, DeploymentStageRecord
from groundwork_contracts.deployment import Deployment
from groundwork_contracts.plan import SealedDeploymentPlan
from groundwork_contracts.readiness import DriftSummary
from groundwork_contracts.tenant import ConversationRecord, CustomerTenant
from groundwork_orchestrator.state.cosmos import (
    CosmosStateStore,
    TenantRegistry,
    TenantScopedRepository,
)

PlanRepository = TenantScopedRepository[SealedDeploymentPlan]
ApprovalRepository = TenantScopedRepository[Approval]
PendingApprovalRepository = TenantScopedRepository[PendingApproval]
DeploymentRepository = TenantScopedRepository[Deployment]
CustomerTenantRepository = TenantScopedRepository[CustomerTenant]
ConversationRepository = TenantScopedRepository[ConversationRecord]
StageRecordRepository = TenantScopedRepository[DeploymentStageRecord]
ReportRepository = TenantScopedRepository[DeploymentReport]
DriftSummaryRepository = TenantScopedRepository[DriftSummary]


def plan_repository(store: CosmosStateStore) -> PlanRepository:
    return store.repository("plans", model_cls=SealedDeploymentPlan, id_field="plan_hash")


def customer_tenant_repository(store: CosmosStateStore) -> CustomerTenantRepository:
    """A tenant's own record lives in its own partition — ``id`` and the partition key are the
    same field, which is exactly right for a record that *is* the tenant, not something scoped
    beneath it."""
    return store.repository("tenants", model_cls=CustomerTenant, id_field="tenant_id")


def conversation_repository(store: CosmosStateStore) -> ConversationRepository:
    return store.repository(
        "conversations", model_cls=ConversationRecord, id_field="conversation_id"
    )


def tenant_registry(store: CosmosStateStore) -> TenantRegistry:
    """The one deliberate cross-partition query this codebase has — see :class:`TenantRegistry`'s
    own docstring for why it exists and why it is safe. Reads the same ``tenants`` container as
    :func:`customer_tenant_repository`, through a completely different, narrower interface."""
    return store.tenant_registry()


def approval_repository(store: CosmosStateStore) -> ApprovalRepository:
    return store.repository("approvals", model_cls=Approval, id_field="approval_id")


def pending_approval_repository(store: CosmosStateStore) -> PendingApprovalRepository:
    """Shares the ``approvals`` container with :func:`approval_repository`.

    Cosmos does not enforce a document shape beyond the partition key; a ``PendingApproval`` and
    the ``Approval`` it eventually becomes occupy the same ``(tenantId, approval_id)`` coordinate
    at different points in that approval's lifecycle, never both at once — see
    ``groundwork_contracts.approval.PendingApproval``'s own docstring for why the two are separate
    types rather than one type in two states.
    """
    return store.repository("approvals", model_cls=PendingApproval, id_field="approval_id")


def deployment_repository(store: CosmosStateStore) -> DeploymentRepository:
    return store.repository("deployments", model_cls=Deployment, id_field="deployment_id")


def stage_record_repository(store: CosmosStateStore) -> StageRecordRepository:
    """Append-only, like the audit repository: a retried stage's earlier attempts are never
    overwritten (FR-030 requires prior attempts retained), only ever added to via a fresh
    ``record_id`` per attempt."""
    return store.repository("stage_records", model_cls=DeploymentStageRecord, id_field="record_id")


def report_repository(store: CosmosStateStore) -> ReportRepository:
    """``id_field="deployment_id"``, deliberately — the original design notes' own indexing
    pattern is "retrieve an archived report by tenantId + deploymentId", not by the report's own
    ``report_id``. A deployment halted, retried, and later succeeded produces more than one
    immutable report blob over its lifetime (T093's own module docstring), but exactly one Cosmos
    metadata document per deployment: the latest report simply replaces the pointer, which is what
    "retrieve the report for this deployment" means to a caller who does not track report history
    themselves. Earlier blobs are never deleted or made unreachable — only which one this
    document currently points to changes."""
    return store.repository("reports", model_cls=DeploymentReport, id_field="deployment_id")


def drift_summary_repository(store: CosmosStateStore) -> DriftSummaryRepository:
    """Latest drift snapshot per ``(tenantId, subscription_id)``.

    One document per subscription within a tenant partition; replacing it advances the current
    readiness signal without retaining every cycle forever.
    """
    return store.repository("drift_summaries", model_cls=DriftSummary, id_field="subscription_id")
