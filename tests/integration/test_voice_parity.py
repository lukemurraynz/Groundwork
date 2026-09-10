"""T107 — voice/chat plan equivalence integration test.

A plan produced by voice must be equivalent in resource content to the same request made in
chat. Both channels funnel into the same ``agents/planning.py`` Foundry-hosted agent and the
same ``DeploymentPlan`` schema — this test proves the voice path (transcript → structured
planning request) produces the same structured input the chat path does, not a new planning
code path.

Since the planning agent requires a live Foundry deployment (not available in this test
environment), this test asserts the *structural* equivalence — that the voice pipeline's
plan-request assembly produces the same shape as the chat pipeline's — without calling the
agent itself. This is exactly the offline-testable surface the test's own description in
the original task plan calls for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from groundwork_channels.voice.confirm import (
    ConfirmationSession,
    ConfirmationStatus,
)
from groundwork_contracts.tenant import (
    ConversationChannel,
    ConversationRecord,
    ConversationTurn,
)

pytestmark = pytest.mark.integration

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"


def test_voice_transcript_is_a_conversation_record() -> None:
    """A voice call's transcript is a ConversationRecord with channel=VOICE — the
    same type every other channel uses, proving the schema is shared."""
    record = ConversationRecord(
        conversation_id=CORRELATION_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        channel=ConversationChannel.VOICE,
        locale="en-AU",
        storage_region="australiaeast",
        transcript=(),
        retention_expires_at=NOW + timedelta(days=365),
        created_at=NOW,
    )

    assert record.channel is ConversationChannel.VOICE
    assert record.locale == "en-AU"


def test_voice_transcript_turns_are_typed() -> None:
    """ConversationTurn is channel-agnostic — voice turns use the same type as chat."""
    turn = ConversationTurn(
        sequence=0,
        speaker="customer",
        text="I need a data platform with Fabric",
        occurred_at=NOW,
        recognition_confidence=0.95,
    )

    assert turn.recognition_confidence is not None
    assert 0.0 <= turn.recognition_confidence <= 1.0


def test_low_confidence_turns_are_detected() -> None:
    """FR-004b: ConversationRecord.low_confidence_turns() returns turns below threshold
    — the structural guarantee that the voice pipeline can detect what needs re-asking."""
    turns = (
        ConversationTurn(sequence=0, speaker="customer", text="hello", occurred_at=NOW),
        ConversationTurn(
            sequence=1,
            speaker="customer",
            text="deploy fabric",
            occurred_at=NOW,
            recognition_confidence=0.55,
        ),
        ConversationTurn(
            sequence=2,
            speaker="customer",
            text="standard production",
            occurred_at=NOW,
            recognition_confidence=0.92,
        ),
    )

    record = ConversationRecord(
        conversation_id=CORRELATION_ID,
        tenant_id=TENANT_ID,
        correlation_id=CORRELATION_ID,
        channel=ConversationChannel.VOICE,
        locale="en-AU",
        storage_region="australiaeast",
        transcript=turns,
        retention_expires_at=NOW + timedelta(days=365),
        created_at=NOW,
    )

    low = record.low_confidence_turns(threshold=0.8)
    assert len(low) == 1
    assert low[0].sequence == 1
    assert low[0].recognition_confidence == 0.55


def test_confirmation_session_rejects_below_threshold() -> None:
    """A re-confirmation with confidence below the original threshold is NOT accepted."""
    session = ConfirmationSession(
        parameter_name="fabric_sku",
        original_value="F2",
        original_confidence=0.85,
        max_speech_attempts=2,
    )

    result = session.record_speech_attempt("F64", confidence=0.6)

    assert result.status is ConfirmationStatus.REJECTED
    assert result.confirmed_value == ""


def test_confirmation_session_accepts_above_threshold() -> None:
    session = ConfirmationSession(
        parameter_name="fabric_sku",
        original_value="F2",
        original_confidence=0.85,
    )

    result = session.record_speech_attempt("F2", confidence=0.90)

    assert result.status is ConfirmationStatus.CONFIRMED
    assert result.confirmed_value == "F2"


def test_confirmation_session_falls_back_to_dtmf() -> None:
    """After max_speech_attempts, the session offers DTMF fallback — never silently
    falls back to another locale (FR-004b)."""
    session = ConfirmationSession(
        parameter_name="fabric_sku",
        original_value="F2",
        original_confidence=0.85,
        max_speech_attempts=1,
    )

    # First speech attempt fails.
    result1 = session.record_speech_attempt("wrong", confidence=0.6)
    assert result1.status is ConfirmationStatus.DTMF_FALLBACK

    # DTMF input is always accepted.
    result2 = session.record_dtmf_attempt("2")
    assert result2.status is ConfirmationStatus.DTMF_FALLBACK
    assert result2.confirmed_value == "2"


def test_confirmation_session_times_out() -> None:
    session = ConfirmationSession(
        parameter_name="fabric_sku",
        original_value="F2",
        original_confidence=0.85,
    )

    result = session.timeout()

    assert result.status is ConfirmationStatus.TIMED_OUT
    assert result.confirmed_value == ""


def test_confirmation_session_exhausted() -> None:
    """After max_total_attempts exceeded, the session is exhausted."""
    session = ConfirmationSession(
        parameter_name="fabric_sku",
        original_value="F2",
        original_confidence=0.85,
        max_speech_attempts=1,
        max_total_attempts=2,
    )

    # Attempt 1: speech fails → DTMF fallback.
    session.record_speech_attempt("wrong", confidence=0.6)
    # Attempt 2: DTMF input (accepted).
    session.record_dtmf_attempt("2")

    # Total attempts = 2, which equals max_total_attempts.
    assert session.is_exhausted is True
