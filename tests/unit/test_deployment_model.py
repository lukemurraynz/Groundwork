"""The Deployment record's state-machine invariants (FR-027, FR-030, FR-035, FR-045b).

Mirrors the style of ``tests/unit/test_audit_authority.py``: each test breaks exactly one rule so a
failure names the invariant that fired.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from groundwork_contracts.audit import AuthorityChain
from groundwork_contracts.deployment import (
    Checkpoint,
    Deployment,
    DeploymentStatus,
    SubscriptionLease,
)
from tests.conftest import (
    APPROVAL_ID,
    CORRELATION_ID,
    DEPLOYMENT_ID,
    SUBSCRIPTION_ID,
    TENANT_ID,
)

PLAN_HASH = "sha256:" + "e" * 64


def _authority() -> AuthorityChain:
    return AuthorityChain(plan_hash=PLAN_HASH, approval_id=APPROVAL_ID)


def _deployment(now: datetime, **overrides: object) -> Deployment:
    kwargs: dict[str, object] = {
        "deployment_id": DEPLOYMENT_ID,
        "tenant_id": TENANT_ID,
        "subscription_id": SUBSCRIPTION_ID,
        "correlation_id": CORRELATION_ID,
        "authority": _authority(),
        "status": DeploymentStatus.QUEUED,
        "queue_position": 0,
    }
    kwargs.update(overrides)
    return Deployment(**kwargs)  # type: ignore[arg-type]


def test_queued_deployment_is_valid(now: datetime) -> None:
    deployment = _deployment(now)
    assert deployment.status is DeploymentStatus.QUEUED
    assert deployment.is_active() is True


def test_executing_requires_a_lease(now: datetime) -> None:
    """FR-045b's per-instance half — see groundwork_contracts.deployment module docstring for why
    the cross-deployment uniqueness half cannot live here."""
    with pytest.raises(ValidationError, match="no lease is recorded"):
        _deployment(
            now,
            status=DeploymentStatus.EXECUTING,
            queue_position=None,
            started_at=now,
        )


def test_executing_with_a_lease_is_valid(now: datetime) -> None:
    lease = SubscriptionLease(holder="orchestrator-worker-7", expires_at=now + timedelta(minutes=5))
    # Not a credential — an opaque resume marker a stage hands itself (see Checkpoint's docstring).
    # ruff's S106 heuristic fires on the parameter name alone; suppressed rather than worked around.
    checkpoint = Checkpoint(
        stage_name="infrastructure",
        resume_token="opaque-token",  # noqa: S106
        recorded_at=now,
    )
    deployment = _deployment(
        now,
        status=DeploymentStatus.EXECUTING,
        queue_position=None,
        started_at=now,
        lease=lease,
        checkpoint=checkpoint,
    )
    assert deployment.is_active() is True


def test_terminal_status_requires_completed_at(now: datetime) -> None:
    with pytest.raises(ValidationError, match="completed_at is missing"):
        _deployment(
            now,
            status=DeploymentStatus.SUCCEEDED,
            queue_position=None,
            started_at=now,
        )


def test_queued_deployment_cannot_have_started(now: datetime) -> None:
    with pytest.raises(ValidationError, match="started_at is set"):
        _deployment(now, started_at=now)


def test_queued_deployment_cannot_have_completed(now: datetime) -> None:
    with pytest.raises(ValidationError, match="completed_at is set"):
        _deployment(now, completed_at=now)


def test_non_queued_status_requires_a_start_time(now: datetime) -> None:
    with pytest.raises(ValidationError, match="started_at is missing"):
        _deployment(
            now,
            status=DeploymentStatus.HALTED,
            queue_position=None,
            completed_at=now,
        )


def test_queue_position_only_valid_while_queued(now: datetime) -> None:
    with pytest.raises(ValidationError, match="queue_position is set"):
        _deployment(
            now,
            status=DeploymentStatus.HALTED,
            started_at=now,
            completed_at=now,
            queue_position=0,
        )


def test_halted_deployment_is_not_active(now: datetime) -> None:
    deployment = _deployment(
        now,
        status=DeploymentStatus.HALTED,
        queue_position=None,
        started_at=now,
        completed_at=now,
    )
    assert deployment.is_active() is False


def test_rolled_back_deployment_is_not_active(now: datetime) -> None:
    deployment = _deployment(
        now,
        status=DeploymentStatus.ROLLED_BACK,
        queue_position=None,
        started_at=now,
        completed_at=now,
    )
    assert deployment.is_active() is False
