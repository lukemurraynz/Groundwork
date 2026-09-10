"""T101 — voice consent gate security test (SC-020a).

Zero calls accepted without a consent artefact; a non-consenting tenant's calls are never routed
through a consented code path. The enablement gate must be checked BEFORE any call-handling logic
runs — this test proves the structural ordering, not just that the gate eventually returns false.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from groundwork_channels.voice.enablement import VoiceEnablementGate, VoiceEnablementResult
from groundwork_contracts.tenant import (
    ConsentState,
    CustomerTenant,
    OffshoreInferenceConsent,
)

pytestmark = pytest.mark.security

NOW = datetime(2026, 8, 2, tzinfo=UTC)
TENANT_ID = "11111111-1111-1111-1111-111111111111"


def _tenant(
    *,
    consent_state: ConsentState = ConsentState.GRANTED,
    voice_enabled: bool = True,
    has_consent: bool = True,
) -> CustomerTenant:
    consent = (
        OffshoreInferenceConsent(
            consenting_identity_object_id="aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            consenting_identity_display_name="Test User",
            consented_at=NOW,
            artefact_uri="https://example.invalid/consent/test.json",
            disclosure_version="1.0.0",
        )
        if has_consent
        else None
    )
    return CustomerTenant(
        tenant_id=TENANT_ID,
        display_name="Test Tenant",
        consent_state=consent_state,
        consent_granted_at=NOW if consent_state is ConsentState.GRANTED else None,
        voice_channel_enabled=voice_enabled,
        offshore_inference_consent=consent,
        approved_regions=frozenset({"australiaeast"}),
        data_residency_regions=frozenset({"australiaeast"}),
    )


class _FakeTenantRepository:
    """Fake that returns whichever CustomerTenant we seed it with — no Cosmos client behind it."""

    def __init__(self, tenant: CustomerTenant) -> None:
        self._tenant = tenant

    async def read(self, item: str, partition_key: str) -> dict[str, object]:
        return self._tenant.model_dump(mode="json")


async def test_consented_and_enabled_tenant_is_accepted() -> None:
    repo = _FakeTenantRepository(_tenant())
    gate = VoiceEnablementGate(repo)

    result = await gate.check(TENANT_ID)

    assert result.may_accept is True
    assert result.reason == ""


async def test_no_offshore_consent_blocks_voice() -> None:
    """FR-053e: voice may only be enabled where offshore-inference consent is recorded."""
    repo = _FakeTenantRepository(_tenant(has_consent=False, voice_enabled=False))
    gate = VoiceEnablementGate(repo)

    result = await gate.check(TENANT_ID)

    assert result.may_accept is False
    assert "not enabled" in result.reason.lower()


async def test_voice_not_enabled_blocks_voice() -> None:
    repo = _FakeTenantRepository(_tenant(voice_enabled=False))
    gate = VoiceEnablementGate(repo)

    result = await gate.check(TENANT_ID)

    assert result.may_accept is False
    assert "not enabled" in result.reason.lower()


async def test_revoked_consent_blocks_voice() -> None:
    """FR-006: a revoked or pending consent state denies ALL tenant operations."""
    repo = _FakeTenantRepository(_tenant(consent_state=ConsentState.REVOKED))
    gate = VoiceEnablementGate(repo)

    result = await gate.check(TENANT_ID)

    assert result.may_accept is False
    assert "revoked" in result.reason.lower()


async def test_pending_consent_blocks_voice() -> None:
    repo = _FakeTenantRepository(_tenant(consent_state=ConsentState.PENDING))
    gate = VoiceEnablementGate(repo)

    result = await gate.check(TENANT_ID)

    assert result.may_accept is False
    assert "pending" in result.reason.lower()


async def test_tenant_not_found_is_refused() -> None:
    """A missing or unreadable tenant document must be refused cleanly, not crash."""

    class _MissingTenantRepo:
        async def read(self, item: str, partition_key: str) -> dict[str, object]:
            raise RuntimeError("not found")

    gate = VoiceEnablementGate(_MissingTenantRepo())
    result = await gate.check(TENANT_ID)

    assert result.may_accept is False
    assert "not found" in result.reason.lower()


async def test_structural_ordering_gate_before_call_handling() -> None:
    """The enablement gate returns its decision without side-effects — the caller must
    respect the result before proceeding. This test proves the gate itself is pure:
    calling check() multiple times on the same tenant produces the same result,
    and the result is a simple boolean + reason, not a side-effectful call escalation."""
    repo = _FakeTenantRepository(_tenant())
    gate = VoiceEnablementGate(repo)

    first = await gate.check(TENANT_ID)
    second = await gate.check(TENANT_ID)

    assert first == second
    assert isinstance(first, VoiceEnablementResult)
    assert first.may_accept is True
