"""T047 — the clarification loop's bookkeeping (FR-002, FR-004b)."""

from __future__ import annotations

import pytest

from groundwork_controlplane.agents.clarify import ClarificationTracker, MaterialParameter

REGION = MaterialParameter(name="region", question="Which Azure region should this deploy to?")
SKU = MaterialParameter(name="fabric_sku", question="What Fabric capacity size do you need?")


def _tracker(**kwargs: object) -> ClarificationTracker:
    return ClarificationTracker((REGION, SKU), **kwargs)  # type: ignore[arg-type]


def test_nothing_observed_means_everything_unresolved() -> None:
    tracker = _tracker()

    assert tracker.is_complete() is False
    assert {p.name for p in tracker.unresolved()} == {"region", "fabric_sku"}


def test_high_confidence_observation_resolves_a_parameter() -> None:
    tracker = _tracker()

    tracker.observe("region", "australiaeast", confidence=0.95)

    assert "region" not in {p.name for p in tracker.unresolved()}
    assert "fabric_sku" in {p.name for p in tracker.unresolved()}


def test_low_confidence_observation_does_not_resolve_a_parameter() -> None:
    """FR-004b — a low-confidence transcription is not silently trusted."""
    tracker = _tracker(confidence_threshold=0.7)

    tracker.observe("region", "australiaeast", confidence=0.4)

    assert "region" in {p.name for p in tracker.unresolved()}


def test_confidence_exactly_at_threshold_resolves() -> None:
    tracker = _tracker(confidence_threshold=0.7)

    tracker.observe("region", "australiaeast", confidence=0.7)

    assert "region" not in {p.name for p in tracker.unresolved()}


def test_no_confidence_reported_is_treated_as_fully_confident() -> None:
    """Typed chat has no recognition confidence to report at all — FR-004b is about speech
    recognition confidence specifically, not a reason to distrust typed input."""
    tracker = _tracker()

    tracker.observe("region", "australiaeast", confidence=None)

    assert "region" not in {p.name for p in tracker.unresolved()}


def test_observing_an_unregistered_parameter_raises() -> None:
    tracker = _tracker()

    with pytest.raises(ValueError, match="not a registered material parameter"):
        tracker.observe("not_a_real_parameter", "value", confidence=1.0)


def test_clarification_resolves_a_low_confidence_parameter() -> None:
    tracker = _tracker()
    tracker.observe("region", "somewhere", confidence=0.2)

    tracker.record_clarification("region", "australiaeast")

    assert "region" not in {p.name for p in tracker.unresolved()}


def test_recorded_clarification_uses_the_parameters_own_question() -> None:
    tracker = _tracker()

    tracker.record_clarification("region", "australiaeast")

    clarifications = tracker.gathered_clarifications()
    assert len(clarifications) == 1
    assert clarifications[0].question == REGION.question
    assert clarifications[0].answer == "australiaeast"


def test_clarifying_an_unregistered_parameter_raises() -> None:
    tracker = _tracker()

    with pytest.raises(ValueError, match="not a registered material parameter"):
        tracker.record_clarification("not_a_real_parameter", "answer")


def test_is_complete_once_every_parameter_is_resolved() -> None:
    tracker = _tracker()
    tracker.observe("region", "australiaeast", confidence=1.0)
    tracker.record_clarification("fabric_sku", "F2")

    assert tracker.is_complete() is True
    assert tracker.unresolved() == ()


def test_invalid_confidence_threshold_is_rejected() -> None:
    with pytest.raises(ValueError, match="confidence_threshold"):
        ClarificationTracker((REGION,), confidence_threshold=1.5)


def test_no_silent_resolution_without_observation_or_clarification() -> None:
    """The structural guarantee this whole module exists for: nothing resolves a parameter except
    the two explicit methods that record where the value came from."""
    tracker = _tracker()

    assert tracker.is_complete() is False
    assert tracker.gathered_clarifications() == ()


# ---------------------------------------------------------------------------
# Standard material parameter constants
# ---------------------------------------------------------------------------


def test_notification_email_constant_is_a_material_parameter() -> None:
    from groundwork_controlplane.agents.clarify import NOTIFICATION_EMAIL

    assert NOTIFICATION_EMAIL.name == "notification_email"
    assert "email" in NOTIFICATION_EMAIL.question.lower()
    assert "authoris" in NOTIFICATION_EMAIL.question.lower()


def test_caller_display_name_constant_is_a_material_parameter() -> None:
    from groundwork_controlplane.agents.clarify import CALLER_DISPLAY_NAME

    assert CALLER_DISPLAY_NAME.name == "caller_display_name"
    assert "name" in CALLER_DISPLAY_NAME.question.lower()


def test_notification_email_as_registered_parameter_requires_explicit_clarification() -> None:
    """FR-002: the email address is material — it must never be silently defaulted."""
    from groundwork_controlplane.agents.clarify import NOTIFICATION_EMAIL

    tracker = ClarificationTracker((NOTIFICATION_EMAIL,))

    assert not tracker.is_complete()
    assert {p.name for p in tracker.unresolved()} == {"notification_email"}

    tracker.record_clarification("notification_email", "admin@contoso.onmicrosoft.com")

    assert tracker.is_complete()
    clarifications = tracker.gathered_clarifications()
    assert len(clarifications) == 1
    assert clarifications[0].answer == "admin@contoso.onmicrosoft.com"


def test_notification_email_reconfirmation_via_low_confidence_then_clarify() -> None:
    """FR-004b: a low-confidence observation (voice recognition) must not resolve the address —
    only an explicit re-ask and answer resolves it."""
    from groundwork_controlplane.agents.clarify import NOTIFICATION_EMAIL

    tracker = ClarificationTracker((NOTIFICATION_EMAIL,), confidence_threshold=0.7)
    tracker.observe("notification_email", "admin@contoso.onmicrosoft.com", confidence=0.3)

    assert not tracker.is_complete()
    assert "notification_email" in {p.name for p in tracker.unresolved()}

    tracker.record_clarification("notification_email", "admin@contoso.onmicrosoft.com")

    assert tracker.is_complete()
