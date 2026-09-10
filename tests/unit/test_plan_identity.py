"""T048 — plan identity hashing and the validity window (FR-012, FR-025)."""

from __future__ import annotations

from datetime import datetime, timedelta

from groundwork_contracts.plan import PLAN_VALIDITY, DeploymentPlan
from groundwork_controlplane.approval.plan_identity import (
    content_matches,
    is_within_validity_window,
    seal_plan,
)
from tests.conftest import REQUESTER_ID, TENANT_ID


def test_seal_plan_binds_tenant_and_requester_from_the_session(
    valid_plan: DeploymentPlan, now: datetime
) -> None:
    sealed = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )

    assert sealed.tenant_id == TENANT_ID
    assert sealed.requesting_identity_object_id == REQUESTER_ID
    assert sealed.plan_hash == valid_plan.content_hash()


def test_sealed_plan_is_valid_at_the_moment_it_was_created(
    valid_plan: DeploymentPlan, now: datetime
) -> None:
    sealed = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )

    assert is_within_validity_window(sealed, now=now) is True


def test_sealed_plan_is_invalid_after_the_validity_window(
    valid_plan: DeploymentPlan, now: datetime
) -> None:
    """FR-012, FR-025 — a plan older than 24 hours is refused, not silently extended."""
    sealed = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )

    later = now + PLAN_VALIDITY + timedelta(minutes=1)
    assert is_within_validity_window(sealed, now=later) is False


def test_sealed_plan_content_matches_its_own_hash(
    valid_plan: DeploymentPlan, now: datetime
) -> None:
    sealed = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )

    assert content_matches(sealed) is True


def test_two_platforms_from_the_same_conversation_produce_the_same_hash(
    valid_plan: DeploymentPlan, now: datetime
) -> None:
    """Identical plan content must hash identically regardless of which session sealed it — the
    hash identifies the plan's content, not the act of sealing."""
    first = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="api",
        now=now,
    )
    second = seal_plan(
        valid_plan,
        tenant_id=TENANT_ID,
        requesting_identity_object_id=REQUESTER_ID,
        requesting_channel="teams",
        now=now,
    )

    assert first.plan_hash == second.plan_hash
