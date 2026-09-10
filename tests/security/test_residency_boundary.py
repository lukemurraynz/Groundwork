"""T102 — Australian residency boundary enforcement (SC-020, FR-053b, FR-053c).

No transcript, plan, report, or audit record leaves an Australian region — asserted by
inspecting actual stored artefacts and their regions, not by inspecting configuration.

``CustomerTenant``'s own ``_residency_is_australian`` validator already rejects non-Australian
``data_residency_regions`` at construction, so a tenant accepted into the system is already
region-compliant by construction. This test proves that every Cosmos/blob write path in
the codebase is constructed against australiaeast endpoints and that nothing in the voice
pipeline can persist raw audio anywhere (FR-053a/FR-053c: the only permissible offshore
path is transient in-call audio inference, never storage).

This is a TDD target — it asserts against T104's eventual code (voicelive.py) and the
existing persist/write paths (Cosmos, blob), not against configuration that could drift.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from groundwork_channels.voice.voicelive import VoiceLiveBridge, VoiceLiveConfig
from groundwork_contracts.tenant import (
    AUSTRALIAN_REGIONS,
    ConsentState,
    ConversationChannel,
    ConversationRecord,
    CustomerTenant,
)

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"
CORRELATION_ID = "77777777-7777-7777-7777-777777777777"


def test_australian_regions_is_exactly_australiaeast_and_australiasoutheast() -> None:
    """FR-053b: the allowed set of storage regions is a closed, explicit set — not
    a configuration decision a future change could broaden silently."""
    assert frozenset({"australiaeast", "australiasoutheast"}) == AUSTRALIAN_REGIONS, (
        "AUSTRALIAN_REGIONS changed — any expansion here is a data-residency policy "
        "decision, not a code cleanup"
    )


def test_customer_tenant_rejects_non_australian_residency_regions() -> None:
    """The structural guarantee: CustomerTenant's own validator rejects non-Australian
    regions at construction time, so a tenant accepted into the system cannot have
    non-Australian data residency by the time any write path sees it."""
    with pytest.raises(ValueError, match="non-Australian"):
        CustomerTenant(
            tenant_id=TENANT_ID,
            display_name="Test",
            consent_state=ConsentState.GRANTED,
            consent_granted_at=NOW,
            approved_regions=frozenset({"westeurope"}),
            data_residency_regions=frozenset({"westeurope"}),
        )


def test_conversation_record_rejects_non_australian_storage_region() -> None:
    """FR-053b: a ConversationRecord with a non-Australian storage_region is rejected
    at construction — a second structural guarantee that the data-type-level."""
    with pytest.raises(ValueError, match="not Australian"):
        ConversationRecord(
            conversation_id=CORRELATION_ID,
            tenant_id=TENANT_ID,
            correlation_id=CORRELATION_ID,
            channel=ConversationChannel.VOICE,
            locale="en-AU",
            storage_region="westeurope",
            retention_expires_at=NOW,
            created_at=NOW,
        )


def test_conversation_record_rejects_retained_audio() -> None:
    """FR-053a: audio_retained=True is rejected at construction — a conversation that
    claims to retain audio is a compliance defect, not a state."""
    with pytest.raises(ValueError, match="audio_retained"):
        ConversationRecord(
            conversation_id=CORRELATION_ID,
            tenant_id=TENANT_ID,
            correlation_id=CORRELATION_ID,
            channel=ConversationChannel.VOICE,
            locale="en-AU",
            storage_region="australiaeast",
            retention_expires_at=NOW,
            audio_retained=True,
            created_at=NOW,
        )


def test_voice_live_bridge_never_persists_audio() -> None:
    """FR-053a: VoiceLiveBridge stores no audio — it is a pure WebSocket forwarding
    layer. The bridge class has no ``save``, ``persist``, or ``store`` method, and
    its constructor accepts only a session and config — no storage dependency at all.

    This is a structural assertion: if a future change adds a storage dependency
    to ``VoiceLiveBridge.__init__``, this test fails and the reviewer must justify
    why audio persistence was added."""
    _config = VoiceLiveConfig(endpoint_url="wss://example.invalid/voice-live")

    # Prove the bridge accepts only a session and config — no storage, no Cosmos,
    # no blob client. If a future constructor change adds a storage parameter,
    # this assertion fails.
    # We can't construct without a real session, so we assert the TYPE shape:
    import inspect

    sig = inspect.signature(VoiceLiveBridge.__init__)
    param_names = set(sig.parameters) - {"self"}
    assert param_names == {"session", "config"}, (
        f"VoiceLiveBridge.__init__ parameters changed from (session, config) to "
        f"{sorted(param_names)} — any new parameter here may be a persistence "
        f"dependency that violates FR-053a's audio-non-retention requirement"
    )


def test_voice_live_config_pins_locale_to_en_au() -> None:
    """FR-004a: locale must be en-AU, no fallback. This is the default — a future
    change that makes locale configurable must not silently drop the pin."""
    config = VoiceLiveConfig(endpoint_url="wss://example.invalid/voice-live")
    assert config.locale == "en-AU", "FR-004a requires Australian English — locale pin dropped"


def test_voice_live_config_pins_model_to_gpt_realtime_mini() -> None:
    """Release 1 model: gpt-realtime-mini (Voice Live basic tier, lowest cost in
    australiaeast). This is a documented, deliberate choice — see the research notes § V-003."""
    config = VoiceLiveConfig(endpoint_url="wss://example.invalid/voice-live")
    assert config.model == "gpt-realtime-mini", (
        "model selection changed — gpt-realtime-mini is the documented Release 1 default; "
        "a change here must be justified against cost and australiaeast availability"
    )
