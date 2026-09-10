"""Contract violation errors.

These exist so that a boundary failure is loud and specific rather than a generic
``ValidationError`` that a caller might be tempted to catch and paper over.

FR-011 requires that a plan failing validation fails the *request*. Nothing in this package
offers a "best effort" or "partial" path, because offering one is how that requirement erodes.
"""

from __future__ import annotations


class ContractViolation(Exception):
    """Base class for every contract boundary failure.

    Deliberately not a subclass of ``ValueError``. A caller writing ``except ValueError`` around
    business logic should not silently swallow a deterministic-execution boundary breach.
    """


class PlanValidationError(ContractViolation):
    """Model output failed to validate as a :class:`DeploymentPlan`.

    Raised at the agent boundary. The correct handling is to fail the request and surface the
    reason. Retrying with a loosened schema, filling in defaults for missing required fields, or
    executing the valid subset are all prohibited by FR-011.
    """

    def __init__(self, detail: str, *, errors: list[str] | None = None) -> None:
        self.detail = detail
        self.errors = errors or []
        super().__init__(detail)


class PlanIntegrityError(ContractViolation):
    """A plan's recomputed content hash does not match its recorded identity.

    Means the plan changed after it was hashed. Because approval binds to the hash (FR-020), an
    integrity failure invalidates any approval against it and requires re-approval (FR-022).
    """

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            "Plan integrity check failed: recorded identity does not match content. "
            "Any approval bound to this plan is void and re-approval is required."
        )
