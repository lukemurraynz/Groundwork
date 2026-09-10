"""The deployment-queue consumption loop (worker.py's own documented gap, closed here).

Nothing before this module actually consumed the ``deployments`` queue — ``api/deployments.py``'s
own docstring already disclosed that admission does not acquire a lease or start execution, and
``engine/sequencer.py``'s own docstring names this exact module as "still to be added". Everything
this loop needs already exists and is independently tested: tenant discovery
(:class:`~groundwork_orchestrator.state.cosmos.TenantRegistry`), per-tenant queued deployments
(:class:`~groundwork_orchestrator.state.repositories.DeploymentRepository`), the per-subscription
lease (:class:`~groundwork_shared.queue.subscription_lease.SubscriptionLeaseStore`), and the stage
sequencer (:class:`~groundwork_orchestrator.engine.sequencer.Sequencer`). This module is only the
wiring between them — decide which queued deployment to attempt next, acquire its lease, transition
it to ``executing``, hand it to the sequencer, and release the lease afterwards.

**One poll iteration never raises.** A single deployment's failure — a missing plan, a lease already
held, a stage that halts, a bug this module has not seen before — must never stop every other
tenant's deployments from being considered on this or a later cycle. :func:`_attempt_deployment`
catches broadly around everything after lease acquisition (the same reasoning
``Sequencer._run_one_stage`` already applies to a raising stage) and always releases the lease in a
``finally``, so a halt or a crash never strands a subscription locked out for the full lease TTL.
:func:`poll_once` never lets one tenant's listing failure stop the rest either.

**A lease-held deployment is skipped, not an error.** FR-045b's whole point is that a second
deployment for the same subscription must not run concurrently — finding a lease already held is the
mechanism working as intended, not a defect. :class:`DeploymentAttemptResult.LEASE_HELD` is a
distinct, expected outcome, never wrapped in :class:`DeploymentAttemptResult.ERROR`.

**Checkpoints and resume need nothing extra here.** ``Sequencer.run`` already reads
``deployment.checkpoint`` and skips completed stages; this module's only obligation is to pass the
:class:`~groundwork_contracts.deployment.Deployment` it actually read from Cosmos (with whatever
real checkpoint it carries), never a freshly constructed one.

**What this module deliberately does not build.** A queued deployment whose plan is missing (data
corruption, or a plan that expired and was reaped from a different container than expected) is
reported as :class:`DeploymentAttemptResult.PLAN_MISSING` and left ``queued`` — it will be retried,
and will fail identically, on every subsequent poll cycle until a human intervenes. There is no
backoff or quarantine mechanism here; building one was not asked for and would be guessing at a
retry policy this session's artefacts do not specify. The same is true of
:class:`DeploymentAttemptResult.ERROR`: logged clearly (secret-scrubbed) and left for the next
cycle, not retried with any particular strategy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from azure.core.credentials_async import AsyncTokenCredential

from groundwork_contracts.deployment import Deployment, DeploymentStatus
from groundwork_contracts.plan import DeploymentPlan
from groundwork_orchestrator.engine.halt import requeue_after_recovery_choice
from groundwork_orchestrator.engine.sequencer import RunResult
from groundwork_orchestrator.state.cosmos import TenantRegistry
from groundwork_orchestrator.state.repositories import (
    CustomerTenantRepository,
    DeploymentRepository,
    PlanRepository,
)
from groundwork_shared.identity.credentials import TenantScopedCredentialFactory
from groundwork_shared.queue.subscription_lease import LeaseHeldError, SubscriptionLeaseStore
from groundwork_shared.telemetry.metrics import record_queue_depth
from groundwork_shared.telemetry.scrubbing import scrub_text

logger = logging.getLogger(__name__)

# Matches the order of magnitude ``InfrastructureStage``'s own ``poll_interval_seconds`` default
# uses (``stages/infrastructure.py``) for polling an Azure deployment stack — there is no existing
# convention specific to queue polling, so this borrows the closest analogous one rather than
# inventing an unrelated cadence.
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


class SequencerLike(Protocol):
    async def run(
        self,
        deployment: Deployment,
        plan: DeploymentPlan,
        *,
        credential: AsyncTokenCredential,
        devops_organization_url: str | None = None,
        fabric_capacity_admin_upn: str | None = None,
    ) -> RunResult: ...


_QUEUED_QUERY = "SELECT * FROM c WHERE c.status = 'queued'"
_EXECUTING_QUERY = "SELECT * FROM c WHERE c.status = 'executing'"


class DeploymentAttemptResult(StrEnum):
    """What happened when this loop tried to move one queued deployment forward.

    Distinct from :class:`~groundwork_contracts.deployment.DeploymentStatus` deliberately: a
    deployment can finish this attempt still ``queued`` (``LEASE_HELD``, ``PLAN_MISSING``,
    ``AWAITING_TENANT_CONFIG``) — this enum is about the *attempt*, not the deployment's own
    state machine.
    """

    EXECUTED = "executed"
    RECOVERED = "recovered"
    LEASE_HELD = "lease_held"
    PLAN_MISSING = "plan_missing"
    AWAITING_TENANT_CONFIG = "awaiting_tenant_config"
    PLATFORM_AT_CAPACITY = "platform_at_capacity"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class DeploymentAttemptOutcome:
    """One deployment's result for one poll cycle — what :func:`poll_once` returns a list of."""

    tenant_id: str
    deployment_id: str
    subscription_id: str
    result: DeploymentAttemptResult
    detail: str = ""
    run_result: RunResult | None = None


async def _attempt_deployment(
    deployment: Deployment,
    *,
    deployment_repository: DeploymentRepository,
    plan_repository: PlanRepository,
    customer_tenant_repository: CustomerTenantRepository,
    lease_store: SubscriptionLeaseStore,
    sequencer: SequencerLike,
    credential_factory: TenantScopedCredentialFactory,
    now_fn: Callable[[], datetime],
    devops_organization_url_fallback: str | None = None,
    fabric_capacity_admin_upn_fallback: str | None = None,
) -> DeploymentAttemptOutcome:
    """Try to move exactly one queued deployment forward.

    Per-deployment engagement-data resolution happens first, before the lease and before any
    write: the tenant record's own ``devops_organization_url`` / ``fabric_capacity_admin_upn``
    take precedence, with the worker-wide settings as fallback. A deployment whose tenant has
    neither source for a required value is declined (:class:`DeploymentAttemptResult.
    AWAITING_TENANT_CONFIG`) and left ``queued`` — a legitimate wait state, not corruption: the
    operator (or a conversation) can record the value and the next poll cycle proceeds.

    Lease acquisition happens next and unconditionally: it is the cheapest possible check, and
    doing it before reading the plan or touching the deployment document means a lease that is
    already held never causes any write at all — the deployment is left exactly as it was found.
    """
    tenant = await customer_tenant_repository.read(deployment.tenant_id, deployment.tenant_id)
    organization_url = (
        tenant.devops_organization_url if tenant is not None else None
    ) or devops_organization_url_fallback
    capacity_admin_upn = (
        tenant.fabric_capacity_admin_upn if tenant is not None else None
    ) or fabric_capacity_admin_upn_fallback
    if organization_url is None or capacity_admin_upn is None:
        missing = ", ".join(
            name
            for name, value in (
                ("devops_organization_url", organization_url),
                ("fabric_capacity_admin_upn", capacity_admin_upn),
            )
            if value is None
        )
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.AWAITING_TENANT_CONFIG,
            detail=f"tenant has no {missing} recorded and no worker-wide fallback is configured",
        )

    try:
        lease = await lease_store.acquire(
            deployment.subscription_id, holder=deployment.deployment_id, now=now_fn()
        )
    except LeaseHeldError as exc:
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.LEASE_HELD,
            detail=scrub_text(str(exc)),
        )

    try:
        sealed = await plan_repository.read(deployment.tenant_id, deployment.authority.plan_hash)
        if sealed is None:
            return DeploymentAttemptOutcome(
                tenant_id=deployment.tenant_id,
                deployment_id=deployment.deployment_id,
                subscription_id=deployment.subscription_id,
                result=DeploymentAttemptResult.PLAN_MISSING,
                detail=(
                    f"plan {deployment.authority.plan_hash} not found in tenant "
                    f"{deployment.tenant_id}'s partition"
                ),
            )

        executing = deployment.model_copy(
            update={
                "status": DeploymentStatus.EXECUTING,
                "queue_position": None,
                "started_at": now_fn(),
                "lease": lease,
            }
        )
        executing = await deployment_repository.replace(deployment.tenant_id, executing)

        credential = credential_factory.scoped_to(deployment.tenant_id).for_tenant(
            deployment.tenant_id
        )
        run_result = await sequencer.run(
            executing,
            sealed.plan,
            credential=credential,
            devops_organization_url=organization_url,
            fabric_capacity_admin_upn=capacity_admin_upn,
        )
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.EXECUTED,
            run_result=run_result,
        )
    except Exception as exc:
        # Anything from here — a stray SequencerError, a Cosmos write failure, a bug this module
        # has not seen before — must not crash the poll loop. Same reasoning as
        # ``Sequencer._run_one_stage`` converting a raised exception into a recorded outcome
        # instead of an uncaught crash; the difference here is there is no stage-record repository
        # to write to, only this outcome to report and log.
        logger.error(
            "deployment execution attempt failed",
            exc_info=True,
            extra={
                "component": "orchestrator",
                "operation": "queue_consumption",
                "tenant_id": deployment.tenant_id,
                "deployment_id": deployment.deployment_id,
            },
        )
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.ERROR,
            detail=scrub_text(f"{type(exc).__name__}: {exc}"),
        )
    finally:
        # Not required for correctness (the lease's own TTL frees a crashed worker's hold), but
        # releasing promptly lets a queued deployment for the same subscription start on the very
        # next poll cycle instead of waiting out the full TTL. Runs whether the attempt above
        # succeeded, halted, or raised — this is the one thing every path through this function
        # must do.
        await lease_store.release(
            deployment.subscription_id, holder=deployment.deployment_id, now=now_fn()
        )


async def _recover_orphaned_executing_deployment(
    deployment: Deployment,
    *,
    deployment_repository: DeploymentRepository,
    lease_store: SubscriptionLeaseStore,
    now_fn: Callable[[], datetime],
) -> DeploymentAttemptOutcome | None:
    """Requeue an ``executing`` deployment only once its subscription lease is truly acquirable.

    The lease store is the single source of truth for expiry semantics and for the one-winner
    take-over race (create-if-absent or etag-guarded replace-if-expired). If ``acquire()`` says the
    lease is still held, this deployment is not orphaned and must be left alone.
    """

    try:
        await lease_store.acquire(
            deployment.subscription_id, holder=deployment.deployment_id, now=now_fn()
        )
    except LeaseHeldError:
        return None

    try:
        current_with_etag = await deployment_repository.read_with_etag(
            deployment.tenant_id, deployment.deployment_id
        )
        if current_with_etag is None:
            return None
        current, etag = current_with_etag
        if current.status is not DeploymentStatus.EXECUTING:
            return None

        requeued = Deployment.model_validate(requeue_after_recovery_choice(current).model_dump())
        await deployment_repository.replace_with_etag(deployment.tenant_id, requeued, etag=etag)
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.RECOVERED,
            detail="requeued orphaned executing deployment after lease expiry or absence",
        )
    except Exception as exc:
        logger.error(
            "orphaned deployment recovery failed",
            exc_info=True,
            extra={
                "component": "orchestrator",
                "operation": "queue_consumption_recovery",
                "tenant_id": deployment.tenant_id,
                "deployment_id": deployment.deployment_id,
            },
        )
        return DeploymentAttemptOutcome(
            tenant_id=deployment.tenant_id,
            deployment_id=deployment.deployment_id,
            subscription_id=deployment.subscription_id,
            result=DeploymentAttemptResult.ERROR,
            detail=scrub_text(f"{type(exc).__name__}: {exc}"),
        )
    finally:
        await lease_store.release(
            deployment.subscription_id, holder=deployment.deployment_id, now=now_fn()
        )


def _log_outcome(outcome: DeploymentAttemptOutcome) -> None:
    extra = {
        "component": "orchestrator",
        "operation": "queue_consumption",
        "tenant_id": outcome.tenant_id,
        "deployment_id": outcome.deployment_id,
        "result": outcome.result.value,
    }
    if outcome.result is DeploymentAttemptResult.EXECUTED:
        final_status = outcome.run_result.deployment.status.value if outcome.run_result else "?"
        logger.info(
            "deployment execution attempt completed",
            extra={**extra, "final_status": final_status},
        )
    elif outcome.result is DeploymentAttemptResult.RECOVERED:
        logger.info("orphaned deployment requeued", extra=extra)
    elif outcome.result is DeploymentAttemptResult.LEASE_HELD:
        logger.debug("deployment attempt skipped: subscription lease held elsewhere", extra=extra)
    else:
        logger.warning(
            "deployment attempt did not execute: %s",
            outcome.detail or outcome.result.value,
            extra=extra,
        )


async def poll_once(
    *,
    tenant_registry: TenantRegistry,
    deployment_repository: DeploymentRepository,
    plan_repository: PlanRepository,
    customer_tenant_repository: CustomerTenantRepository,
    lease_store: SubscriptionLeaseStore,
    sequencer: SequencerLike,
    credential_factory: TenantScopedCredentialFactory,
    now_fn: Callable[[], datetime],
    devops_organization_url_fallback: str | None = None,
    fabric_capacity_admin_upn_fallback: str | None = None,
    max_concurrent_deployments: int | None = None,
) -> list[DeploymentAttemptOutcome]:
    """One full pass: every onboarded tenant, every one of that tenant's queued deployments.

    Queued deployments within a tenant are attempted in ``queue_position`` order (FIFO fairness) —
    not strictly required for correctness (nothing here enforces admission order across tenants
    either), but reasonable and cheap given the list is already in hand. A deployment with no
    ``queue_position`` recorded sorts first rather than raising, since the field is genuinely
    optional even while queued (see ``Deployment``'s own docstring).

    ``max_concurrent_deployments``, when given, is a platform-wide ceiling on how many deployments
    may be EXECUTING at once, across every tenant — distinct from both existing concurrency
    controls, which bound different things: ``CustomerTenant.concurrency_cap`` limits one tenant's
    own active count (enforced at admission time, ``queue/admission.py``), and the per-subscription
    lease (``SubscriptionLeaseStore``) only ever serialises writes to one subscription, with no
    opinion on how many *different* subscriptions run at once. Nothing previously bounded that
    total — ``GROUNDWORK_MAX_CONCURRENT_DEPLOYMENTS`` was required at provision time and validated
    into settings, but never read by this loop (found live 2026-09-07).

    A soft ceiling, not a hard one, and deliberately so given how this loop actually runs (WAF
    assessment §4.3): each pod's own ``poll_once`` call is fully sequential —
    ``_attempt_deployment`` awaits a queued deployment's entire ``Sequencer.run`` to completion
    before the next one in this pod's own loop is even considered — so real cross-tenant
    concurrency here comes only from
    multiple orchestrator pod *replicas* each running an independent ``run_forever`` loop against
    the same Cosmos state. ``executing_count`` starts from a real, shared read (``_EXECUTING_QUERY``
    reflects every pod's committed writes) and is then incremented locally as this pod's own pass
    starts new work — correct for this pod, but another replica's poll cycle reading the same
    baseline concurrently could independently decide it also has headroom, so two replicas can
    together exceed the cap by a small margin in the window between reads. Acceptable for a
    soft operational ceiling; not a substitute for a real distributed semaphore if this ever needs
    to be an exact, enforced limit.

    Two passes over the tenant list, not one interleaved pass, because the cap has to be checked
    against the *true* current total: attempting each tenant's queued deployments as soon as that
    tenant is reached would under-count deployments already executing for tenants not yet visited
    this cycle. Recovery always runs regardless of the cap in either pass: an orphaned EXECUTING
    deployment did not increase real concurrency (its worker is gone), so recovering it is
    reducing load, never new work the cap exists to bound.
    """
    outcomes: list[DeploymentAttemptOutcome] = []
    tenant_ids = [tenant_id async for tenant_id in tenant_registry.list_tenant_ids()]

    # Pass 1: recover orphans and gather each tenant's queued list, tallying the platform-wide
    # executing count as we go — this is the only pass that touches _EXECUTING_QUERY.
    executing_count = 0
    per_tenant_queued: list[tuple[str, list[Deployment], set[str]]] = []
    for tenant_id in tenant_ids:
        recovered_deployment_ids: set[str] = set()
        async for deployment in deployment_repository.query(tenant_id, _EXECUTING_QUERY):
            outcome = await _recover_orphaned_executing_deployment(
                deployment,
                deployment_repository=deployment_repository,
                lease_store=lease_store,
                now_fn=now_fn,
            )
            if outcome is None:
                executing_count += 1
                continue
            if outcome.result is DeploymentAttemptResult.RECOVERED:
                recovered_deployment_ids.add(outcome.deployment_id)
            _log_outcome(outcome)
            outcomes.append(outcome)

        queued: list[Deployment] = []
        async for deployment in deployment_repository.query(tenant_id, _QUEUED_QUERY):
            queued.append(deployment)
        queued.sort(key=lambda d: d.queue_position if d.queue_position is not None else -1)
        # T086a/FR-054: the signal alert-gw-queue-depth (observability.bicep) queries. Sampled
        # once per tenant per poll cycle — a point-in-time gauge, not an average over the cycle.
        record_queue_depth(tenant_id=tenant_id, depth=len(queued))
        per_tenant_queued.append((tenant_id, queued, recovered_deployment_ids))

    # Pass 2: attempt each tenant's queued deployments, now that executing_count reflects every
    # tenant, not just the ones visited so far.
    for _tenant_id, queued, recovered_deployment_ids in per_tenant_queued:
        for deployment in queued:
            if deployment.deployment_id in recovered_deployment_ids:
                continue
            if (
                max_concurrent_deployments is not None
                and executing_count >= max_concurrent_deployments
            ):
                outcome = DeploymentAttemptOutcome(
                    tenant_id=deployment.tenant_id,
                    deployment_id=deployment.deployment_id,
                    subscription_id=deployment.subscription_id,
                    result=DeploymentAttemptResult.PLATFORM_AT_CAPACITY,
                    detail=(
                        f"platform is at its configured concurrency ceiling "
                        f"({max_concurrent_deployments} executing); left queued for a later cycle"
                    ),
                )
                _log_outcome(outcome)
                outcomes.append(outcome)
                continue
            outcome = await _attempt_deployment(
                deployment,
                deployment_repository=deployment_repository,
                plan_repository=plan_repository,
                customer_tenant_repository=customer_tenant_repository,
                lease_store=lease_store,
                sequencer=sequencer,
                credential_factory=credential_factory,
                now_fn=now_fn,
                devops_organization_url_fallback=devops_organization_url_fallback,
                fabric_capacity_admin_upn_fallback=fabric_capacity_admin_upn_fallback,
            )
            if outcome.result is DeploymentAttemptResult.EXECUTED:
                executing_count += 1
            _log_outcome(outcome)
            outcomes.append(outcome)

    # FR-054 sibling: one structured heartbeat per poll cycle. observability.bicep's
    # queue-poll-heartbeat alert fires on its ABSENCE - silence is the only reliable signal that
    # this loop (and therefore orphan recovery and execution) has stopped, since a hung cycle
    # emits no failure of its own.
    logger.info(
        "queue_poll_cycle",
        extra={
            "event": "queue_poll_cycle",
            "component": "orchestrator",
            "operation": "queue_consumption_poll",
            "tenants_considered": len({o.tenant_id for o in outcomes}),
            "outcomes": len(outcomes),
        },
    )
    return outcomes


async def run_forever(
    *,
    tenant_registry: TenantRegistry,
    deployment_repository: DeploymentRepository,
    plan_repository: PlanRepository,
    customer_tenant_repository: CustomerTenantRepository,
    lease_store: SubscriptionLeaseStore,
    sequencer: SequencerLike,
    credential_factory: TenantScopedCredentialFactory,
    now_fn: Callable[[], datetime],
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    devops_organization_url_fallback: str | None = None,
    fabric_capacity_admin_upn_fallback: str | None = None,
    max_concurrent_deployments: int | None = None,
) -> None:
    """Poll indefinitely, until the calling task is cancelled.

    Meant to be run as a background ``asyncio.Task`` from ``worker.py``'s lifespan and cancelled on
    shutdown. ``asyncio.CancelledError`` is a ``BaseException``, not caught by the broad
    ``except Exception`` below, so ``task.cancel()`` unwinds this loop cleanly through
    ``asyncio.sleep`` without needing special-case handling here.
    """
    while True:
        try:
            await poll_once(
                tenant_registry=tenant_registry,
                deployment_repository=deployment_repository,
                plan_repository=plan_repository,
                customer_tenant_repository=customer_tenant_repository,
                lease_store=lease_store,
                sequencer=sequencer,
                credential_factory=credential_factory,
                now_fn=now_fn,
                devops_organization_url_fallback=devops_organization_url_fallback,
                fabric_capacity_admin_upn_fallback=fabric_capacity_admin_upn_fallback,
                max_concurrent_deployments=max_concurrent_deployments,
            )
        except Exception:
            # poll_once already catches per-deployment; this is a last-resort guard against a
            # failure in tenant listing itself (e.g. a transient Cosmos error enumerating the
            # `tenants` container) so one bad cycle cannot kill the loop's own asyncio.Task.
            logger.error(
                "queue poll iteration failed",
                exc_info=True,
                extra={"component": "orchestrator", "operation": "queue_consumption"},
            )
        await asyncio.sleep(poll_interval_seconds)
