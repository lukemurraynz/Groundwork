"""The clarification loop's bookkeeping (T047; FR-002, FR-004b).

FR-002 requires an explicit question for any missing or low-confidence *material* parameter — one
this platform has no safe default for — rather than silently assuming a value. This module is the
decision logic behind that requirement: given what has been observed so far in a conversation, which
parameters remain unresolved, and what to ask about each.

It is deliberately not a conversation engine. It has no I/O, holds no session state across turns
beyond what is fed into it, and asks nothing itself — the agent (T045) drives the actual dialogue
and calls into this tracker after each turn. What it owns is the one invariant FR-002 actually
cares about: **a parameter is never resolved except by being explicitly observed with sufficient
confidence, or by being explicitly asked about and answered.** There is no third path.
"""

from __future__ import annotations

from dataclasses import dataclass

from groundwork_contracts.plan import Clarification

DEFAULT_CONFIDENCE_THRESHOLD = 0.7


@dataclass(frozen=True, slots=True)
class MaterialParameter:
    """One parameter this platform has no safe default for.

    'Material' means exactly that — not every field on a plan needs its own clarifying question,
    only the ones where guessing wrong would be a real problem (target region, subscription,
    Fabric capacity SKU), which is why this is a caller-supplied, per-blueprint set rather than
    something this module infers from ``DeploymentPlan`` itself.
    """

    name: str
    question: str


# ---------------------------------------------------------------------------
# Standard material parameters
#
# Callers register a subset of these with ``ClarificationTracker`` — not all
# are relevant on every channel. ``NOTIFICATION_EMAIL`` should be registered
# for every conversation: the address is needed for deployment outcomes *and*
# for the multi-tenant app authorisation link, so it must be gathered and
# explicitly reconfirmed before planning completes (FR-002, FR-004b).
# ``CALLER_DISPLAY_NAME`` is registered for voice sessions where the telephony
# handler cannot resolve an authenticated display name from the call leg.
# ---------------------------------------------------------------------------

NOTIFICATION_EMAIL = MaterialParameter(
    name="notification_email",
    question=(
        "What email address should we send notifications and the multi-tenant app authorisation "
        "link to? Please spell it out if you're on a voice call — I'll read it back to confirm."
    ),
)
CALLER_DISPLAY_NAME = MaterialParameter(
    name="caller_display_name",
    question="What is your name? I'll use it to identify you throughout this call.",
)


@dataclass(slots=True)
class _Observation:
    value: str
    confidence: float | None


class ClarificationTracker:
    """Tracks which of a plan's material parameters are resolved, and what remains to ask about."""

    def __init__(
        self,
        required: tuple[MaterialParameter, ...],
        *,
        confidence_threshold: float = DEFAULT_CONFIDENCE_THRESHOLD,
    ) -> None:
        if not 0.0 <= confidence_threshold <= 1.0:
            raise ValueError(f"confidence_threshold must be in [0, 1], got {confidence_threshold}")
        self._required = {parameter.name: parameter for parameter in required}
        self._threshold = confidence_threshold
        self._observations: dict[str, _Observation] = {}
        self._clarifications: list[Clarification] = []

    def observe(self, parameter_name: str, value: str, confidence: float | None) -> None:
        """Record what was heard for ``parameter_name``, with its recognition confidence.

        ``confidence=None`` means the source does not report one at all — typed chat, as opposed to
        speech recognition — and is treated as fully confident. FR-004b's low-confidence
        re-confirmation requirement is specifically about *recognition* confidence, which only
        exists for a channel that transcribes speech.
        """
        if parameter_name not in self._required:
            raise ValueError(f"{parameter_name!r} is not a registered material parameter")
        self._observations[parameter_name] = _Observation(value=value, confidence=confidence)

    def record_clarification(
        self, parameter_name: str, answer: str, *, confidence: float | None = None
    ) -> None:
        """Record that ``parameter_name``'s clarifying question was asked and answered.

        The only path by which a missing or low-confidence parameter becomes resolved. There is no
        method on this class that marks a parameter resolved without a recorded question and
        answer — that is what makes "no silent defaulting" a structural property of this tracker
        rather than a convention callers are trusted to follow.
        """
        parameter = self._required.get(parameter_name)
        if parameter is None:
            raise ValueError(f"{parameter_name!r} is not a registered material parameter")
        self._clarifications.append(
            Clarification(question=parameter.question, answer=answer, confidence=confidence)
        )
        self._observations[parameter_name] = _Observation(
            value=answer, confidence=confidence if confidence is not None else 1.0
        )

    def _is_resolved(self, name: str) -> bool:
        observation = self._observations.get(name)
        if observation is None:
            return False
        if observation.confidence is None:
            return True
        return observation.confidence >= self._threshold

    def unresolved(self) -> tuple[MaterialParameter, ...]:
        """Material parameters that still need an explicit clarifying question (FR-002)."""
        return tuple(
            parameter for name, parameter in self._required.items() if not self._is_resolved(name)
        )

    def is_complete(self) -> bool:
        return not self.unresolved()

    def gathered_clarifications(self) -> tuple[Clarification, ...]:
        """The audit trail of what was asked and answered.

        Feeds ``DeploymentPlan.clarifications_gathered`` once the plan is constructed.
        """
        return tuple(self._clarifications)
