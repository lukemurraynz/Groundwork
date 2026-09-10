"""T070/T071 — the stage-sequencing engine.

Uses the real ``standard-production-fabric`` blueprint (its dependency-resolved order is exactly
what the sequencer must honour) with fake :class:`Stage` implementations — the same reasoning as
every other real-Azure-adjacent module this session fakes at its own boundary: a stage's *real*
Azure work (T076-T083) does not exist yet and is not this module's concern; what this module owns
is the sequencing, checkpointing, and halt behaviour around whatever a stage reports.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from azure.cosmos.exceptions import CosmosResourceNotFoundError

from groundwork_contracts.audit import (
    AuditRecord,
    AuthorityChain,
    DeploymentStageRecord,
    StageError,
    StageStatus,
)
from groundwork_contracts.deployment import (
    Checkpoint,
    Deployment,
    DeploymentStatus,
    SubscriptionLease,
)
from groundwork_orchestrator.engine.preview import WhatIfCaptureError
from groundwork_orchestrator.engine.sequencer import (
    RunResult,
    Sequencer,
    SequencerError,
    StageExecutionContext,
    StageOutcome,
)
from groundwork_orchestrator.state.audit_repository import AuditRepository
from groundwork_orchestrator.state.cosmos import TENANT_PARTITION_FIELD, TenantScopedRepository
from groundwork_shared.config.blueprints import load_blueprint
from tests.conftest import APPROVAL_ID, SUBSCRIPTION_ID, TENANT_ID

CORRELATION_ID = "77777777-7777-7777-7777-777777777777"
DEPLOYMENT_ID = "88888888-8888-8888-8888-888888888888"
ACTOR_OBJECT_ID = "99999999-9999-9999-9999-999999999999"
PLAN_HASH = "sha256:" + "a" * 64
NOW = datetime(2026, 8, 1, tzinfo=UTC)

STAGE_ORDER = (
    "devops_project",
    "infrastructure",
    "identity",
    "networking",
    "fabric",
    "monitoring",
    "validation_tests",
)


class _FakeContainer:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    async def create_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def upsert_item(self, body: dict[str, Any], **_: Any) -> Mapping[str, Any]:
        self.documents[body["id"]] = dict(body)
        return body

    async def read_item(self, item: str, partition_key: Any, **_: Any) -> Mapping[str, Any]:
        found = self.documents.get(item)
        if found is None:
            raise CosmosResourceNotFoundError(status_code=404, message="not found")
        return found

    async def query_items(
        self,
        query: str = "",
        *,
        parameters: list[dict[str, Any]] | None = None,
        partition_key: Any = None,
        **_: Any,
    ) -> AsyncIterator[Mapping[str, Any]]:
        """Filters by ``parameters`` against matching document fields (``@field`` -> ``field``),
        matching every ``SELECT * FROM c WHERE c.field = @field [AND ...]`` query this codebase
        actually issues (``approval/lookup.py``, ``engine/retry.py``). Ignoring ``parameters``
        entirely — this fake's original behaviour — was fine while nothing queried
        ``stage_record_repository`` with a filter; T072's ``load_stage_attempt_history`` does, and
        a fake that returns every stage's records regardless of ``stage_name`` silently breaks
        retry's own minimum-retry-interval check by picking up unrelated stages' timestamps."""
        for doc in self.documents.values():
            if partition_key is not None and doc.get(TENANT_PARTITION_FIELD) != partition_key:
                continue
            if parameters and not all(
                doc.get(param["name"].lstrip("@")) == param["value"] for param in parameters
            ):
                continue
            yield doc


def _clock(moment: datetime = NOW) -> list[datetime]:
    """A monotonically-advancing fake clock: ``_now_fn`` consumes this list in order, so each
    call to the sequencer's injected ``now_fn`` returns the next scheduled instant."""
    return [moment + timedelta(seconds=i) for i in range(200)]


def _now_fn(ticks: list[datetime]) -> Any:
    index = {"i": 0}

    def _next() -> datetime:
        value = ticks[index["i"]]
        index["i"] += 1
        return value

    return _next


class _FakeStage:
    def __init__(self, name: str, outcomes: list[StageOutcome]) -> None:
        self.name = name
        self._outcomes = list(outcomes)
        self.calls: list[StageExecutionContext] = []

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        self.calls.append(context)
        return self._outcomes.pop(0)


_RESUME_TOKEN = "ok"  # noqa: S105 -- an opaque stage-resume marker, not a credential


def _succeeding_stages() -> dict[str, _FakeStage]:
    return {
        name: _FakeStage(
            name, [StageOutcome(status=StageStatus.SUCCEEDED, resume_token=_RESUME_TOKEN)]
        )
        for name in STAGE_ORDER
    }


@pytest.fixture
def blueprint():
    manifest = (
        Path(__file__).resolve().parents[2]
        / "infra"
        / "blueprints"
        / "standard-production-fabric"
        / "blueprint.yaml"
    )
    return load_blueprint(manifest)


def _deployment(
    *, status: DeploymentStatus = DeploymentStatus.EXECUTING, **overrides: object
) -> Deployment:
    kwargs: dict[str, object] = {
        "deployment_id": DEPLOYMENT_ID,
        "tenant_id": TENANT_ID,
        "subscription_id": SUBSCRIPTION_ID,
        "correlation_id": CORRELATION_ID,
        "authority": AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID),
        "status": status,
    }
    if status is DeploymentStatus.EXECUTING:
        kwargs["started_at"] = NOW
        kwargs["lease"] = SubscriptionLease(
            holder=DEPLOYMENT_ID, expires_at=NOW + timedelta(hours=1)
        )
    kwargs.update(overrides)
    return Deployment(**kwargs)  # type: ignore[arg-type]


class _FakeWhatIfCapture:
    """A synthetic what-if capture that either succeeds, fails, or raises — the same pattern as
    _FakeStage, so sequencer tests can exercise the pseudo-stage's own halt/retry/recovery paths
    without a real ARM endpoint behind them."""

    def __init__(self, outcomes: list[str | Exception]) -> None:
        """Each entry is either a URL string (success) or an exception (failure)."""
        self._outcomes = list(outcomes)
        self.calls: list[tuple[object, object]] = []

    async def capture(self, *, deployment: object, plan: object, credential: object) -> str:
        self.calls.append((deployment, plan))
        next_outcome = self._outcomes.pop(0)
        if isinstance(next_outcome, Exception):
            raise next_outcome
        return next_outcome


def _sequencer(
    blueprint,
    stages: dict[str, _FakeStage],
    ticks: list[datetime],
    *,
    what_if_capture: _FakeWhatIfCapture | None = None,
    policy_preflight: object | None = None,
    cost_preflight: object | None = None,
    tenant_repository: object | None = None,
) -> tuple[Sequencer, TenantScopedRepository[Deployment], _FakeContainer]:
    deployment_container = _FakeContainer()
    deployment_repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )
    stage_record_repository = TenantScopedRepository(
        _FakeContainer(), model_cls=DeploymentStageRecord, id_field="record_id"
    )
    audit_container = _FakeContainer()
    audit_repo_backing: TenantScopedRepository[AuditRecord] = TenantScopedRepository(
        audit_container, model_cls=AuditRecord, id_field="audit_id"
    )
    audit_repository = AuditRepository(audit_repo_backing)

    if what_if_capture is None:
        what_if_capture = _FakeWhatIfCapture(["https://example.invalid/whatif/ok.json"])

    sequencer = Sequencer(
        blueprint,
        stages,  # type: ignore[arg-type]
        what_if_capture,  # type: ignore[arg-type]
        deployment_repository=deployment_repository,
        stage_record_repository=stage_record_repository,
        audit_repository=audit_repository,
        actor_object_id=ACTOR_OBJECT_ID,
        now_fn=_now_fn(ticks),
        tenant_repository=tenant_repository,  # type: ignore[arg-type]
        policy_preflight=policy_preflight,  # type: ignore[arg-type]
        cost_preflight=cost_preflight,  # type: ignore[arg-type]
    )
    return sequencer, deployment_repository, audit_container


class _FakeCredential:
    pass


def test_missing_stage_implementation_refuses_construction(blueprint) -> None:
    stages = _succeeding_stages()
    del stages["fabric"]

    with pytest.raises(ValueError, match="fabric"):
        _sequencer(blueprint, stages, _clock())


async def test_run_requires_the_deployment_to_already_be_executing(blueprint, valid_plan) -> None:
    sequencer, _repo, _audit = _sequencer(blueprint, _succeeding_stages(), _clock())
    queued = _deployment(status=DeploymentStatus.QUEUED)

    with pytest.raises(SequencerError):
        await sequencer.run(queued, valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]


async def test_full_successful_run_executes_every_stage_in_order(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, audit_container = _sequencer(blueprint, stages, _clock())
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert isinstance(result, RunResult)
    assert result.halted is None
    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert result.deployment.completed_at is not None
    assert result.deployment.current_stage is None

    for name in STAGE_ORDER:
        assert len(stages[name].calls) == 1, f"{name} was not called exactly once"

    # Every stage produced exactly one audit record.
    assert len(audit_container.documents) == len(STAGE_ORDER)


async def test_a_failed_stage_halts_and_skips_downstream_stages(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    error = StageError(code="ResourceGroupConflict", message="already exists", is_transient=False)
    stages["identity"] = _FakeStage(
        "identity", [StageOutcome(status=StageStatus.FAILED, error=error)]
    )
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())
    deployment = _deployment()

    result = await sequencer.run(deployment, valid_plan, credential=_FakeCredential())

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "identity"
    assert result.halted.error == error

    # Stages before the failure ran; the failing stage ran; nothing after it did.
    assert len(stages["devops_project"].calls) == 1
    assert len(stages["infrastructure"].calls) == 1
    assert len(stages["identity"].calls) == 1
    assert len(stages["networking"].calls) == 0
    assert len(stages["fabric"].calls) == 0
    assert len(stages["monitoring"].calls) == 0
    assert len(stages["validation_tests"].calls) == 0


class _RaisingStage:
    """A stage that raises instead of returning a FAILED outcome — the real-world case, since a
    live Azure call can fail in ways its own author never enumerated."""

    def __init__(self, name: str, exc: Exception) -> None:
        self.name = name
        self._exc = exc
        self.calls: list[StageExecutionContext] = []

    async def execute(self, context: StageExecutionContext) -> StageOutcome:
        self.calls.append(context)
        raise self._exc


async def test_a_stage_that_raises_halts_instead_of_crashing_the_run(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    stages["networking"] = _RaisingStage("networking", ConnectionError("could not reach ARM"))
    sequencer, _repo, audit_container = _sequencer(blueprint, stages, _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "networking"
    assert result.halted.error.code == "ConnectionError"
    assert "could not reach ARM" in result.halted.error.message
    assert result.halted.error.is_transient is False
    # Stages after the raising one never ran, exactly as with a returned FAILED outcome.
    assert len(stages["fabric"].calls) == 0
    # identity runs in parallel with networking (both depend only on infrastructure) and still
    # succeeds — only stages strictly downstream of the failure are skipped.
    assert len(stages["identity"].calls) == 1
    # The raising stage still produced a real audit record — it was authorised before it ran,
    # regardless of how it ended.
    # devops_project, infrastructure, identity, networking
    assert len(audit_container.documents) == 4


async def test_halted_infrastructure_failure_offers_rollback(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    error = StageError(code="DeploymentStackConflict", message="drift detected", is_transient=False)
    stages["infrastructure"] = _FakeStage(
        "infrastructure", [StageOutcome(status=StageStatus.FAILED, error=error)]
    )
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert "rollback" in result.halted.recovery_options


async def test_halted_devops_project_failure_does_not_offer_rollback(blueprint, valid_plan) -> None:
    """devops_project's blueprint.yaml prose never mentions rollback — creating a DevOps project
    is forward-fixable, not something FR-031a's rollback gate applies to.

    ``is_transient=False`` (a 403 is permanent — retrying with the same inputs fails identically,
    unlike a network blip) so this halts on the very first attempt rather than retrying per T072 —
    this test is about the halted state's own recovery options, not retry behaviour, which
    `test_a_transient_failure_retries_instead_of_halting_immediately` covers separately."""
    stages = _succeeding_stages()
    error = StageError(code="OrganizationUnreachable", message="403", is_transient=False)
    stages["devops_project"] = _FakeStage(
        "devops_project", [StageOutcome(status=StageStatus.FAILED, error=error)]
    )
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())

    assert result.halted is not None
    assert "rollback" not in result.halted.recovery_options
    assert result.halted.recovery_options == ("retry", "forward_fix")


async def test_resume_skips_stages_already_completed_before_the_checkpoint(
    blueprint, valid_plan
) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())

    resuming = _deployment(
        checkpoint=Checkpoint(stage_name="identity", resume_token=_RESUME_TOKEN, recorded_at=NOW)
    )

    result = await sequencer.run(resuming, valid_plan, credential=_FakeCredential())

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    # devops_project, infrastructure, and identity were already done before the checkpoint.
    assert len(stages["devops_project"].calls) == 0
    assert len(stages["infrastructure"].calls) == 0
    assert len(stages["identity"].calls) == 0
    # Everything after the checkpointed stage still ran.
    assert len(stages["networking"].calls) == 1
    assert len(stages["fabric"].calls) == 1
    assert len(stages["monitoring"].calls) == 1
    assert len(stages["validation_tests"].calls) == 1


def test_stage_outcome_rejects_started_status() -> None:
    with pytest.raises(ValueError, match="STARTED"):
        StageOutcome(status=StageStatus.STARTED)


def test_stage_outcome_rejects_failed_without_error() -> None:
    with pytest.raises(ValueError, match="FAILED outcome must carry an error"):
        StageOutcome(status=StageStatus.FAILED)


# ---------------------------------------------------------------------------
# T074 — what-if pseudo-stage tests
# ---------------------------------------------------------------------------

WHAT_IF_URL = "https://example.invalid/whatif/ok.json"


async def test_what_if_succeeds_and_proceeds_to_real_stages(blueprint, valid_plan) -> None:
    """A successful what-if capture sets what_if_artefact_uri and the deployment continues
    through the normal stage loop."""
    stages = _succeeding_stages()
    what_if = _FakeWhatIfCapture([WHAT_IF_URL])
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), what_if_capture=what_if)

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert result.deployment.what_if_artefact_uri == WHAT_IF_URL
    assert result.halted is None
    # The capture was called exactly once, before any real stage.
    assert len(what_if.calls) == 1
    # Every real stage still ran.
    for name in STAGE_ORDER:
        assert len(stages[name].calls) == 1


async def test_what_if_permanent_failure_halts(blueprint, valid_plan) -> None:
    """A permanently-failing what-if capture halts before any real stage runs."""
    stages = _succeeding_stages()
    error = WhatIfCaptureError("ARM what-if rejected: policy violation")
    what_if = _FakeWhatIfCapture([error])
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), what_if_capture=what_if)

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "what_if_preview"
    assert result.halted.error.code == "WhatIfCaptureError"
    assert result.halted.recovery_options == ("retry", "forward_fix")
    # No real stage ever ran.
    for name in STAGE_ORDER:
        assert len(stages[name].calls) == 0


async def test_what_if_transient_failure_requeues(blueprint, valid_plan) -> None:
    """A transiently-failing what-if capture within budget requeues rather than halting."""
    import httpx

    stages = _succeeding_stages()
    # httpx.TransportError is classified as transient by is_transient_error.
    what_if = _FakeWhatIfCapture([httpx.TransportError("network blip")])
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), what_if_capture=what_if)

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.QUEUED
    assert result.halted is None
    # No real stage ever ran — the deployment was requeued before reaching them.
    for name in STAGE_ORDER:
        assert len(stages[name].calls) == 0


async def test_resumed_deployment_never_re_attempts_what_if(blueprint, valid_plan) -> None:
    """A deployment with a checkpoint (resuming) must skip the what-if capture entirely."""
    stages = _succeeding_stages()
    what_if = _FakeWhatIfCapture([WHAT_IF_URL])
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), what_if_capture=what_if)

    resuming = _deployment(
        checkpoint=Checkpoint(stage_name="identity", resume_token="ok", recorded_at=NOW)  # noqa: S106
    )
    result = await sequencer.run(resuming, valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    # The what-if capture was never called — the checkpoint means it was already done.
    assert len(what_if.calls) == 0


async def test_existing_what_if_artefact_prevents_re_capture(blueprint, valid_plan) -> None:
    """A deployment that already has a what_if_artefact_uri (from a prior partial run) must
    never re-attempt the capture — idempotence."""
    stages = _succeeding_stages()
    what_if = _FakeWhatIfCapture([WHAT_IF_URL])
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock(), what_if_capture=what_if)

    already_captured = _deployment(what_if_artefact_uri=WHAT_IF_URL)
    result = await sequencer.run(
        already_captured,
        valid_plan,
        credential=_FakeCredential(),  # type: ignore[arg-type]
    )

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert result.deployment.what_if_artefact_uri == WHAT_IF_URL
    # The composite key: what_if_artefact_uri was already set, so capture was skipped.
    assert len(what_if.calls) == 0
    # But real stages still ran.
    for name in STAGE_ORDER:
        assert len(stages[name].calls) == 1


# ---------------------------------------------------------------------------
# T075 — policy preflight interceptor tests
# ---------------------------------------------------------------------------


async def _passing_preflight(_ctx: object) -> tuple[object, str]:
    from groundwork_contracts.readiness import ValidationStatus

    return ValidationStatus.PASSED, "ok"


async def _failing_preflight(_ctx: object) -> tuple[object, str]:
    from groundwork_contracts.readiness import ValidationStatus

    return ValidationStatus.FAILED, "found 1 Deny policy: BlockResources"


async def _raising_preflight(_ctx: object) -> tuple[object, str]:
    from groundwork_orchestrator.engine.preflight import PolicyPreflightError

    raise PolicyPreflightError("could not reach ARM")


async def test_preflight_passing_proceeds_to_infrastructure(blueprint, valid_plan) -> None:
    """A passing preflight does not block the infrastructure stage."""
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        policy_preflight=_passing_preflight,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(stages["infrastructure"].calls) == 1


async def test_preflight_failing_halts_before_infrastructure(blueprint, valid_plan) -> None:
    """A failing preflight halts the deployment before infrastructure runs, attributed to its own
    pseudo-stage (not "infrastructure" — infrastructure itself never ran, and misattributing the
    halt to it would make `recovery_options_for_stage` incorrectly offer rollback, since
    infrastructure's own blueprint prose names it as an option)."""
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        policy_preflight=_failing_preflight,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "policy_preflight"
    assert "found 1 Deny policy" in result.halted.error.message
    assert result.halted.recovery_options == ("retry", "forward_fix")
    # Infrastructure never ran.
    assert len(stages["infrastructure"].calls) == 0
    # But stages before infrastructure did.
    assert len(stages["devops_project"].calls) == 1


async def test_preflight_failure_is_durably_reconstructable(blueprint, valid_plan) -> None:
    """A preflight halt must leave a real DeploymentStageRecord behind — not just a transient
    RunResult.halted this one call happens to return. engine/halt.py's reconstruct_halted_view
    (used by GET /deployments and api/recovery.py) reconstructs a halt reason from the
    most-recently-ended stage record; a preflight halt with no record at all would reconstruct to
    "nothing to report" for any caller other than this exact Sequencer.run() invocation — the bug
    this test guards against (found and fixed 2026-08-02: T075's policy preflight originally
    halted without ever writing a stage record)."""
    stage_record_container = _FakeContainer()
    stage_record_repository: TenantScopedRepository[DeploymentStageRecord] = TenantScopedRepository(
        stage_record_container, model_cls=DeploymentStageRecord, id_field="record_id"
    )
    deployment_container = _FakeContainer()
    deployment_repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )
    audit_repository = AuditRepository(
        TenantScopedRepository(_FakeContainer(), model_cls=AuditRecord, id_field="audit_id")
    )
    sequencer = Sequencer(
        blueprint,
        _succeeding_stages(),  # type: ignore[arg-type]
        _FakeWhatIfCapture(["https://example.invalid/whatif/ok.json"]),
        deployment_repository=deployment_repository,
        stage_record_repository=stage_record_repository,
        audit_repository=audit_repository,
        actor_object_id=ACTOR_OBJECT_ID,
        now_fn=_now_fn(_clock()),
        policy_preflight=_failing_preflight,  # type: ignore[arg-type]
    )

    await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    from groundwork_orchestrator.engine.halt import load_all_stage_records, reconstruct_halted_view

    records = await load_all_stage_records(stage_record_repository, TENANT_ID, DEPLOYMENT_ID)
    halted_view = reconstruct_halted_view(records, blueprint)
    assert halted_view is not None
    assert halted_view.failing_stage == "policy_preflight"
    assert halted_view.recovery_options == ("retry", "forward_fix")


async def test_preflight_raising_halts_with_unreachable(blueprint, valid_plan) -> None:
    """A preflight that raises (can't reach ARM) halts with the error surfaced."""
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        policy_preflight=_raising_preflight,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert "could not reach ARM" in result.halted.error.message


async def test_no_preflight_configured_is_skipped(blueprint, valid_plan) -> None:
    """When policy_preflight is None (default), the infrastructure stage runs normally."""
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(stages["infrastructure"].calls) == 1


# ---------------------------------------------------------------------------
# T075a — cost re-approval preflight interceptor tests
# ---------------------------------------------------------------------------


async def _cost_within_band(_plan: object, *, now: object) -> object:
    from groundwork_orchestrator.engine.cost_preflight import CostRecheckResult

    return CostRecheckResult(
        within_band=True,
        recomputed_monthly_total=412.50,
        approved_monthly_total=412.50,
        band_lower=371.25,
        band_upper=495.0,
        crosses_second_approver_threshold=False,
    )


async def _cost_outside_band(_plan: object, *, now: object) -> object:
    from groundwork_orchestrator.engine.cost_preflight import CostRecheckResult

    return CostRecheckResult(
        within_band=False,
        recomputed_monthly_total=820.0,
        approved_monthly_total=412.50,
        band_lower=371.25,
        band_upper=495.0,
        crosses_second_approver_threshold=False,
    )


async def _cost_outside_band_escalating(_plan: object, *, now: object) -> object:
    from groundwork_orchestrator.engine.cost_preflight import CostRecheckResult

    return CostRecheckResult(
        within_band=False,
        recomputed_monthly_total=150_000.0,
        approved_monthly_total=412.50,
        band_lower=371.25,
        band_upper=495.0,
        crosses_second_approver_threshold=True,
    )


async def _cost_raising(_plan: object, *, now: object) -> object:
    from groundwork_orchestrator.engine.cost_preflight import CostPreflightError

    raise CostPreflightError("retail prices API unreachable")


async def test_cost_within_band_proceeds_to_infrastructure(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        cost_preflight=_cost_within_band,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(stages["infrastructure"].calls) == 1


async def test_cost_outside_band_halts_before_infrastructure(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        cost_preflight=_cost_outside_band,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "cost_reapproval"
    assert result.halted.error.code == "CostReapprovalRequired"
    # forward_fix is deliberately not offered — nothing can be "forward-fixed" without a fresh
    # human approval.
    assert result.halted.recovery_options == ("retry",)
    assert len(stages["infrastructure"].calls) == 0


async def test_cost_outside_band_escalating_records_escalation_in_error_code(
    blueprint, valid_plan
) -> None:
    """api/recovery.py gates a cost_reapproval retry on the StageError.code to decide whether the
    fresh approval must itself carry a second_approval — this is the one place that decision is
    recorded, so it must survive from halt through to reconstruction."""
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        cost_preflight=_cost_outside_band_escalating,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.halted is not None
    assert result.halted.error.code == "CostReapprovalEscalationRequired"


async def test_cost_preflight_raising_halts(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(
        blueprint,
        stages,
        _clock(),
        cost_preflight=_cost_raising,  # type: ignore[arg-type]
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert result.halted is not None
    assert result.halted.failing_stage == "cost_reapproval"
    assert "retail prices API unreachable" in result.halted.error.message


async def test_no_cost_preflight_configured_is_skipped(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    sequencer, _repo, _audit = _sequencer(blueprint, stages, _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED


# ---------------------------------------------------------------------------
# T092/T093 — report generation on terminal states
# ---------------------------------------------------------------------------


class _FakeReportArchiveStore:
    def __init__(self) -> None:
        self.archived: list[object] = []

    async def archive(self, content: object, *, now: object) -> object:
        from groundwork_contracts.audit import DeploymentReport

        self.archived.append(content)
        return DeploymentReport(
            report_id="dddddddd-dddd-dddd-dddd-dddddddddddd",
            deployment_id=content.deployment_id,  # type: ignore[attr-defined]
            tenant_id=content.tenant_id,  # type: ignore[attr-defined]
            correlation_id=content.correlation_id,  # type: ignore[attr-defined]
            authority=content.authority,  # type: ignore[attr-defined]
            outcome=content.outcome,  # type: ignore[attr-defined]
            stage_summary=content.stage_summary,  # type: ignore[attr-defined]
            resources_created=content.resources_created,  # type: ignore[attr-defined]
            iac_artefact_versions=content.iac_artefact_versions,  # type: ignore[attr-defined]
            final_monthly_cost_aud=content.final_monthly_cost_aud,  # type: ignore[attr-defined]
            blob_uri="https://example.invalid/reports/report.json",
            content_hash="sha256:" + "0" * 64,
            generated_at=NOW,
            retention_expires_at=NOW + timedelta(days=365),
        )


class _RaisingReportArchiveStore:
    async def archive(self, content: object, *, now: object) -> object:
        raise RuntimeError("blob storage unreachable")


class _FakeNotifier:
    def __init__(self) -> None:
        self.notified: list[tuple[object, object]] = []

    async def notify_deployment_outcome(
        self, report: object, plan: object, *, tenant_id: str, now: object
    ) -> None:
        self.notified.append((report, plan))


class _RaisingNotifier:
    async def notify_deployment_outcome(
        self, report: object, plan: object, *, tenant_id: str, now: object
    ) -> None:
        raise RuntimeError("email provider unreachable")


def _sequencer_with_reports(
    blueprint,
    stages: dict[str, _FakeStage],
    ticks: list[datetime],
    *,
    report_archive_store: object,
    notifier: object | None = None,
) -> tuple[Sequencer, TenantScopedRepository[Deployment], TenantScopedRepository]:
    from groundwork_contracts.audit import DeploymentReport

    deployment_container = _FakeContainer()
    deployment_repository: TenantScopedRepository[Deployment] = TenantScopedRepository(
        deployment_container, model_cls=Deployment, id_field="deployment_id"
    )
    stage_record_repository = TenantScopedRepository(
        _FakeContainer(), model_cls=DeploymentStageRecord, id_field="record_id"
    )
    report_container = _FakeContainer()
    report_repository: TenantScopedRepository[DeploymentReport] = TenantScopedRepository(
        report_container, model_cls=DeploymentReport, id_field="deployment_id"
    )
    audit_repository = AuditRepository(
        TenantScopedRepository(_FakeContainer(), model_cls=AuditRecord, id_field="audit_id")
    )
    sequencer = Sequencer(
        blueprint,
        stages,  # type: ignore[arg-type]
        _FakeWhatIfCapture(["https://example.invalid/whatif/ok.json"]),
        deployment_repository=deployment_repository,
        stage_record_repository=stage_record_repository,
        audit_repository=audit_repository,
        actor_object_id=ACTOR_OBJECT_ID,
        now_fn=_now_fn(ticks),
        report_archive_store=report_archive_store,  # type: ignore[arg-type]
        report_repository=report_repository,
        notifier=notifier,  # type: ignore[arg-type]
    )
    return sequencer, deployment_repository, report_repository


async def test_successful_run_generates_and_persists_a_report(blueprint, valid_plan) -> None:
    store = _FakeReportArchiveStore()
    sequencer, _repo, report_repository = _sequencer_with_reports(
        blueprint, _succeeding_stages(), _clock(), report_archive_store=store
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(store.archived) == 1
    persisted = await report_repository.read(TENANT_ID, DEPLOYMENT_ID)
    assert persisted is not None


async def test_halted_run_generates_and_persists_a_report(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    stages["infrastructure"] = _FakeStage(
        "infrastructure",
        [
            StageOutcome(
                status=StageStatus.FAILED,
                error=StageError(code="Boom", message="failed", is_transient=False),
            )
        ],
    )
    store = _FakeReportArchiveStore()
    sequencer, _repo, report_repository = _sequencer_with_reports(
        blueprint, stages, _clock(), report_archive_store=store
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert len(store.archived) == 1
    assert store.archived[0].outcome.value == "halted"
    persisted = await report_repository.read(TENANT_ID, DEPLOYMENT_ID)
    assert persisted is not None


async def test_no_report_store_configured_is_skipped(blueprint, valid_plan) -> None:
    """Matches policy_preflight/cost_preflight's own optional-by-default pattern."""
    sequencer, _repo, _audit = _sequencer(blueprint, _succeeding_stages(), _clock())

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED


async def test_report_generation_failure_does_not_crash_the_run(blueprint, valid_plan) -> None:
    """A report is valuable but not authoritative — a blob-storage outage must not turn an
    otherwise-successful (or cleanly-halted) run into an unhandled crash."""
    sequencer, _repo, _ = _sequencer_with_reports(
        blueprint, _succeeding_stages(), _clock(), report_archive_store=_RaisingReportArchiveStore()
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED


async def test_successful_run_notifies_when_a_notifier_is_configured(blueprint, valid_plan) -> None:
    """T096/FR-051: notification fires after a successful archive, only if injected — see
    ``NotifierLike``'s own docstring for why no real caller wires one yet."""
    store = _FakeReportArchiveStore()
    notifier = _FakeNotifier()
    sequencer, _repo, _reports = _sequencer_with_reports(
        blueprint, _succeeding_stages(), _clock(), report_archive_store=store, notifier=notifier
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(notifier.notified) == 1
    notified_report, notified_plan = notifier.notified[0]
    assert notified_report.outcome.value == "succeeded"  # type: ignore[attr-defined]
    assert notified_plan is valid_plan


async def test_halted_run_notifies_when_a_notifier_is_configured(blueprint, valid_plan) -> None:
    stages = _succeeding_stages()
    stages["infrastructure"] = _FakeStage(
        "infrastructure",
        [
            StageOutcome(
                status=StageStatus.FAILED,
                error=StageError(code="Boom", message="failed", is_transient=False),
            )
        ],
    )
    store = _FakeReportArchiveStore()
    notifier = _FakeNotifier()
    sequencer, _repo, _reports = _sequencer_with_reports(
        blueprint, stages, _clock(), report_archive_store=store, notifier=notifier
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.HALTED
    assert len(notifier.notified) == 1
    notified_report, _plan = notifier.notified[0]
    assert notified_report.outcome.value == "halted"  # type: ignore[attr-defined]


async def test_no_notifier_configured_means_no_notification_attempted(
    blueprint, valid_plan
) -> None:
    """Matches report_archive_store/policy_preflight/cost_preflight's own optional-by-default
    pattern — a report is still generated even with no notifier injected."""
    store = _FakeReportArchiveStore()
    sequencer, _repo, report_repository = _sequencer_with_reports(
        blueprint, _succeeding_stages(), _clock(), report_archive_store=store
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    assert len(store.archived) == 1
    persisted = await report_repository.read(TENANT_ID, DEPLOYMENT_ID)
    assert persisted is not None


async def test_notification_failure_does_not_crash_the_run(blueprint, valid_plan) -> None:
    """A notification is valuable but not authoritative — an email-provider outage must not turn
    an otherwise-successful, already-persisted-report run into an unhandled crash."""
    store = _FakeReportArchiveStore()
    sequencer, _repo, report_repository = _sequencer_with_reports(
        blueprint,
        _succeeding_stages(),
        _clock(),
        report_archive_store=store,
        notifier=_RaisingNotifier(),
    )

    result = await sequencer.run(_deployment(), valid_plan, credential=_FakeCredential())  # type: ignore[arg-type]

    assert result.deployment.status is DeploymentStatus.SUCCEEDED
    persisted = await report_repository.read(TENANT_ID, DEPLOYMENT_ID)
    assert persisted is not None
