"""Plan identity hashing and the validity window (T048; FR-012).

The hashing and validity-window mechanics already live on
:class:`~groundwork_contracts.plan.SealedDeploymentPlan` — ``content_hash()``, ``seal()``, and
``ValidityWindow`` — because they are deterministic-execution boundary invariants that must hold
with zero I/O and be testable without a control plane running at all (see that module's own
docstring). This
module is the thin control-plane service wrapping them: the one place a validated
:class:`~groundwork_contracts.plan.DeploymentPlan` is actually bound to an authenticated session and
becomes the durable object the rest of the system reasons about.

Not a re-implementation. Duplicating ``seal()``'s hashing logic here would create exactly the kind
of "two sources of truth about plan identity" FR-012 exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, datetime

from groundwork_contracts.plan import DeploymentPlan, SealedDeploymentPlan


def seal_plan(
    plan: DeploymentPlan,
    *,
    tenant_id: str,
    requesting_identity_object_id: str,
    requesting_channel: str,
    now: datetime | None = None,
) -> SealedDeploymentPlan:
    """Bind a validated plan to the authenticated session that requested it (FR-007, FR-012).

    ``tenant_id`` and ``requesting_identity_object_id`` must come from the authenticated session —
    never from the plan or from conversation content. There is deliberately no code path here, or
    on ``DeploymentPlan`` itself, that could derive either from the plan's own fields (see
    ``plan.py``'s module docstring for why ``DeploymentPlan`` has no ``tenantId`` field at all).
    """
    return SealedDeploymentPlan.seal(
        plan,
        tenant_id=tenant_id,
        requesting_identity_object_id=requesting_identity_object_id,
        requesting_channel=requesting_channel,
        now=now,
    )


def is_within_validity_window(sealed: SealedDeploymentPlan, *, now: datetime | None = None) -> bool:
    """Whether ``sealed`` may still be acted on (FR-012, FR-025).

    FR-025 requires an expired plan to be refused rather than silently re-validated or extended —
    this is the single check every call site (validate, approve, execute) must make before doing
    anything with a plan older than its validity window allows.
    """
    moment = now or datetime.now(UTC)
    return sealed.validity.is_valid_at(moment)


def content_matches(sealed: SealedDeploymentPlan) -> bool:
    """Whether ``sealed.plan_hash`` still matches ``sealed.plan``'s actual content.

    Always ``True`` for any ``SealedDeploymentPlan`` that exists in memory —
    ``_hash_matches_content`` on that type raises ``PlanIntegrityError`` at construction if it does
    not, so a mismatched instance can never exist to be checked. This function exists for the case
    that matters: a plan reconstructed from durable storage (Cosmos), where the same
    constructor-time check applies and a caller wants to describe *why* a read failed rather than
    let ``PlanIntegrityError`` propagate as an unhandled exception at the repository boundary.
    """
    return sealed.plan.content_hash() == sealed.plan_hash
