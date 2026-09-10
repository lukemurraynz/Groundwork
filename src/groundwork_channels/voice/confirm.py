"""Low-confidence parameter re-confirmation and DTMF fallback (T105; FR-004b).

Never silently falls back to another locale — FR-004b requires a failed confirmation to surface
as an explicit re-ask, not a silent default. When a Voice Live turn is recognised below the
configured confidence threshold, the caller is asked to re-confirm the parameter. If speech
recognition fails repeatedly or the caller prefers, DTMF digit input is offered as an accessible
fallback (no speech recognition required).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ConfirmationStatus(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    TIMED_OUT = "timed_out"
    DTMF_FALLBACK = "dtmf_fallback"


@dataclass(frozen=True, slots=True)
class ConfirmationResult:
    """The outcome of one re-confirmation attempt for a low-confidence parameter."""

    status: ConfirmationStatus
    parameter_name: str
    confirmed_value: str = ""
    attempts: int = 1


@dataclass
class ConfirmationSession:
    """Tracks re-confirmation state for one parameter across multiple attempts.

    ``max_speech_attempts`` is the number of speech re-asks before falling back to DTMF —
    after that, the caller must use digit input. ``max_total_attempts`` is the hard ceiling
    across both speech and DTMF; exceeding it means the parameter could not be confirmed.
    """

    parameter_name: str
    original_value: str
    original_confidence: float
    max_speech_attempts: int = 2
    max_total_attempts: int = 5
    speech_attempts: int = 0
    dtmf_attempts: int = 0
    _confirmed_value: str = ""
    _status: ConfirmationStatus = ConfirmationStatus.REJECTED

    @property
    def needs_dtmf_fallback(self) -> bool:
        """Whether speech re-confirmation has been exhausted and DTMF should be offered."""
        return self.speech_attempts >= self.max_speech_attempts and self._status not in (
            ConfirmationStatus.CONFIRMED,
            ConfirmationStatus.TIMED_OUT,
        )

    @property
    def is_exhausted(self) -> bool:
        """Whether all attempts (speech + DTMF) have been used."""
        total = self.speech_attempts + self.dtmf_attempts
        return total >= self.max_total_attempts

    def record_speech_attempt(self, recognised_value: str, confidence: float) -> ConfirmationResult:
        """Record one speech re-confirmation attempt. If confidence >= original, accept it."""
        self.speech_attempts += 1
        if confidence >= self.original_confidence:
            self._confirmed_value = recognised_value
            self._status = ConfirmationStatus.CONFIRMED
        elif self.needs_dtmf_fallback:
            self._status = ConfirmationStatus.DTMF_FALLBACK
        elif self.is_exhausted:
            self._status = ConfirmationStatus.REJECTED
        else:
            self._status = ConfirmationStatus.REJECTED
        return ConfirmationResult(
            status=self._status,
            parameter_name=self.parameter_name,
            confirmed_value=(
                self._confirmed_value if self._status is ConfirmationStatus.CONFIRMED else ""
            ),
            attempts=self.speech_attempts + self.dtmf_attempts,
        )

    def record_dtmf_attempt(self, digits: str) -> ConfirmationResult:
        """Record a DTMF digit input attempt. DTMF is always accepted as confirmed — it is
        a deliberate manual input, not a recognition guess."""
        self.dtmf_attempts += 1
        self._confirmed_value = digits
        self._status = ConfirmationStatus.CONFIRMED
        return ConfirmationResult(
            status=ConfirmationStatus.DTMF_FALLBACK,
            parameter_name=self.parameter_name,
            confirmed_value=digits,
            attempts=self.speech_attempts + self.dtmf_attempts,
        )

    def timeout(self) -> ConfirmationResult:
        """The caller did not respond within the re-confirmation window."""
        self._status = ConfirmationStatus.TIMED_OUT
        return ConfirmationResult(
            status=ConfirmationStatus.TIMED_OUT,
            parameter_name=self.parameter_name,
            attempts=self.speech_attempts + self.dtmf_attempts,
        )
