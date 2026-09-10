"""Voice-channel enablement gate (T100; FR-053e, SC-020a).

Refuses a call for any tenant without recorded consent and with voice disabled, directing them to
chat with no capability loss (FR-004d). Never default-allow — the gate must be the first check
the call path hits, before any telephony or Voice Live connection is opened.

Consent is checked via :class:`~groundwork_contracts.tenant.CustomerTenant.may_accept_voice_call`,
which gates on three conditions — ``consent_state`` must be GRANTED, ``voice_channel_enabled``
must be True, and ``offshore_inference_consent`` must be present. Any one failing means voice is
unavailable.

:class:`VoiceEnablementGate` is an injectable Protocol — a test can substitute a fake without
needing a real Cosmos client, the same seam every other gate in this codebase uses
(``PreflightCheck``, ``NotifierLike``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from groundwork_contracts.tenant import CustomerTenant


@dataclass(frozen=True, slots=True)
class VoiceEnablementResult:
    """Whether an inbound call may be accepted, and why not."""

    may_accept: bool
    reason: str = ""
    """Empty when ``may_accept`` is True — a refusal always carries a reason."""


class VoiceEnablementGateLike(Protocol):
    """Injectable seam — tests supply a fake with no Cosmos client behind it."""

    async def check(self, tenant_id: str) -> VoiceEnablementResult: ...


class VoiceEnablementGate:
    """Reads a :class:`CustomerTenant` from Cosmos and applies its own
    :meth:`~groundwork_contracts.tenant.CustomerTenant.may_accept_voice_call` check.

    ``tenant_repository`` is a :class:`~groundwork_orchestrator.state.cosmos.TenantScopedRepository`
    scoped to the ``CustomerTenant`` type — it must accept the same ``read`` interface that
    repository already provides for every other tenant-scoped read in this codebase."""

    def __init__(self, tenant_repository: Any) -> None:
        # Protocol-shaped, not typed as TenantScopedRepository directly, so the test fake doesn't
        # need to match every method that class actually carries — only ``read`` is needed.
        self._tenant_repository = tenant_repository

    async def check(self, tenant_id: str) -> VoiceEnablementResult:
        try:
            doc: dict[str, object] = await self._tenant_repository.read(tenant_id, tenant_id)
            tenant = CustomerTenant.model_validate(doc)
        except Exception:
            return VoiceEnablementResult(
                may_accept=False, reason="tenant not found or not onboarded"
            )

        if not tenant.consent_state.permits_tenant_operations:
            return VoiceEnablementResult(
                may_accept=False,
                reason=(
                    f"tenant consent state is {tenant.consent_state.value!r} -- must be 'granted'"
                ),
            )

        if not tenant.voice_channel_enabled:
            return VoiceEnablementResult(
                may_accept=False,
                reason="voice channel is not enabled for this tenant; please use chat",
            )

        if tenant.offshore_inference_consent is None:
            return VoiceEnablementResult(
                may_accept=False,
                reason="offshore inference consent has not been recorded for this tenant; "
                "voice is unavailable without it (FR-053d)",
            )

        # All three conditions satisfied — this is exactly what may_accept_voice_call() checks.
        return VoiceEnablementResult(may_accept=True)
