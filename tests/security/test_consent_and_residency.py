"""Consent and data residency — FR-006, FR-053b/c/d/e, PD-005.

The residency split resolved by CL-009 is enforced in code, with no Azure control behind it. A
change that persists an inference artefact would breach FR-053b silently, so these tests assert on
the shape of what can be *stored*, not on configuration intent.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from pydantic import ValidationError

from groundwork_contracts import (
    ConsentState,
    ConversationChannel,
    ConversationRecord,
    ConversationTurn,
    CustomerTenant,
    DataClassification,
    OffshoreInferenceConsent,
    SubscriptionEntitlement,
)
from tests.conftest import (
    APPROVER_ID,
    CORRELATION_ID,
    PLAN_ID,
    SUBSCRIPTION_ID,
    TENANT_ID,
)

pytestmark = pytest.mark.security


def _consent(now: datetime) -> OffshoreInferenceConsent:
    return OffshoreInferenceConsent(
        consenting_identity_object_id=APPROVER_ID,
        consenting_identity_display_name="Platform Admin",
        consented_at=now,
        artefact_uri="https://example.invalid/consent/1",
        disclosure_version="1.0.0",
    )


def _tenant(now: datetime, **overrides: object) -> CustomerTenant:
    kwargs: dict[str, object] = {
        "tenant_id": TENANT_ID,
        "display_name": "Example Customer",
        "consent_state": ConsentState.GRANTED,
        "consent_granted_at": now,
        "subscriptions": (
            SubscriptionEntitlement(
                subscription_id=SUBSCRIPTION_ID,
                display_name="Data Platform Prod",
                may_deploy=True,
            ),
        ),
        "approved_regions": frozenset({"australiaeast"}),
        "data_residency_regions": frozenset({"australiaeast"}),
    }
    kwargs.update(overrides)
    return CustomerTenant(**kwargs)  # type: ignore[arg-type]


def _conversation(now: datetime, **overrides: object) -> ConversationRecord:
    kwargs: dict[str, object] = {
        "conversation_id": PLAN_ID,
        "tenant_id": TENANT_ID,
        "correlation_id": CORRELATION_ID,
        "channel": ConversationChannel.TEAMS,
        "storage_region": "australiaeast",
        "retention_expires_at": now + timedelta(days=365),
        "created_at": now,
    }
    kwargs.update(overrides)
    return ConversationRecord(**kwargs)  # type: ignore[arg-type]


# --- Application consent (FR-006) -------------------------------------------------


def test_granted_consent_requires_a_timestamp(now: datetime) -> None:
    """The audit trail needs to know when authority was granted (FR-047)."""
    with pytest.raises(ValidationError, match="consent_granted_at is missing"):
        _tenant(now, consent_granted_at=None)


@pytest.mark.parametrize(
    ("state", "permitted"),
    [
        (ConsentState.GRANTED, True),
        (ConsentState.PENDING, False),
        (ConsentState.REVOKED, False),
    ],
)
def test_only_granted_consent_permits_operations(state: ConsentState, permitted: bool) -> None:
    """FR-006 — pending and revoked both deny. There is no partial grant."""
    assert state.permits_tenant_operations is permitted


def test_entitlement_lookup_returns_none_for_unauthorised_subscription(
    now: datetime,
) -> None:
    """FR-008 — a caller may only reach subscriptions the tenant authorised.

    None means not entitled. There is deliberately no default that could fabricate one.
    """
    tenant = _tenant(now)
    assert tenant.entitlement_for(SUBSCRIPTION_ID) is not None
    assert tenant.entitlement_for("00000000-0000-0000-0000-000000000000") is None


# --- Voice consent (FR-053d, FR-053e, SC-020a) ------------------------------------


def test_voice_cannot_be_enabled_without_consent(now: datetime) -> None:
    """FR-053d — voice requires explicit recorded per-tenant consent.

    Enforced in the type, so no code path can enable voice without it.
    """
    with pytest.raises(ValidationError, match="without offshore_inference_consent"):
        _tenant(now, voice_channel_enabled=True)


def test_voice_enabled_with_consent_is_accepted(now: datetime) -> None:
    tenant = _tenant(now, voice_channel_enabled=True, offshore_inference_consent=_consent(now))
    assert tenant.may_accept_voice_call() is True


def test_calls_refused_without_consent(now: datetime) -> None:
    """SC-020a — zero calls accepted for a tenant without a consent artefact."""
    assert _tenant(now).may_accept_voice_call() is False


def test_calls_refused_when_application_consent_revoked(now: datetime) -> None:
    """Offshore consent does not survive loss of application consent.

    Both gates must hold: revoking the application grant removes all authority, including voice.
    """
    tenant = _tenant(
        now,
        consent_state=ConsentState.REVOKED,
        consent_granted_at=now,
        voice_channel_enabled=True,
        offshore_inference_consent=_consent(now),
    )
    assert tenant.may_accept_voice_call() is False


def test_voice_defaults_to_disabled(now: datetime) -> None:
    """Default-deny, not default-allow. An omitted flag must not enable a channel."""
    tenant = _tenant(now)
    assert tenant.voice_channel_enabled is False
    assert tenant.offshore_inference_consent is None


# --- Residency (FR-053b, SC-020) --------------------------------------------------


def test_non_australian_residency_is_rejected(now: datetime) -> None:
    """FR-053b — reject at construction, not when the first transcript is written."""
    with pytest.raises(ValidationError, match="non-Australian region"):
        _tenant(now, data_residency_regions=frozenset({"eastus"}))


def test_conversation_outside_australia_is_rejected(now: datetime) -> None:
    with pytest.raises(ValidationError, match="not Australian"):
        _conversation(now, storage_region="westeurope")


def test_retained_audio_is_a_defect(now: datetime) -> None:
    """FR-053a — raw audio is discarded at transcription.

    ``audio_retained`` exists to be asserted False, not to permit True. Under PD-005 the offshore
    inference exception covers in-flight audio only; retaining it would turn a transient exposure
    into a stored one.
    """
    with pytest.raises(ValidationError, match="audio_retained is True"):
        _conversation(now, audio_retained=True)


def test_classification_cannot_be_downgraded(now: datetime) -> None:
    """FR-053 fixes conversation content as Confidential."""
    with pytest.raises(ValidationError, match="classified Confidential"):
        _conversation(now, classification=DataClassification.INTERNAL)


def test_retention_cannot_exceed_policy(now: datetime) -> None:
    """FR-053a — 12 months. A longer expiry silently extends retention of Confidential content."""
    with pytest.raises(ValidationError, match="exceeds the 365-day period"):
        _conversation(now, retention_expires_at=now + timedelta(days=400))


def test_locale_is_pinned_to_en_au(now: datetime) -> None:
    """FR-004a/FR-004b — no fallback locale can be introduced silently."""
    with pytest.raises(ValidationError):
        _conversation(now, locale="en-US")


def test_low_confidence_turns_are_identifiable(now: datetime) -> None:
    """FR-004b — low-confidence recognition must trigger re-confirmation, not assumption."""
    record = _conversation(
        now,
        channel=ConversationChannel.VOICE,
        transcript=(
            ConversationTurn(
                sequence=0,
                speaker="customer",
                text="Deploy to Australia East",
                occurred_at=now,
                recognition_confidence=0.95,
            ),
            ConversationTurn(
                sequence=1,
                speaker="customer",
                text="subscription three three three",
                occurred_at=now,
                recognition_confidence=0.42,
            ),
        ),
    )
    low = record.low_confidence_turns()
    assert len(low) == 1
    assert low[0].sequence == 1


def test_transcript_must_be_ordered(now: datetime) -> None:
    with pytest.raises(ValidationError, match="not in sequence order"):
        _conversation(
            now,
            transcript=(
                ConversationTurn(sequence=5, speaker="customer", text="second", occurred_at=now),
                ConversationTurn(sequence=1, speaker="customer", text="first", occurred_at=now),
            ),
        )
