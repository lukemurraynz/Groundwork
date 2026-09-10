"""The schema-boundary adapter (T046) — the deterministic-execution boundary (ADR-0001), FR-011.

This is the one place raw model output is turned into a :class:`DeploymentPlan` or rejected
outright. FR-011 is explicit that a plan failing validation fails the *request* — there is no
partial acceptance, no filling in a missing required field with a default, and no retry against a
loosened schema. ``validate_model_output`` either returns a fully valid plan or raises
:class:`~groundwork_contracts.errors.PlanValidationError`; there is no third outcome.

The function is almost trivially thin on purpose. The strictness lives in ``DeploymentPlan``
itself (``extra="forbid"``, the executable-content rejection in ``PlanResource``, the
forbidden-field absence documented in ``plan.py``'s module docstring) — this module's job is only
to be the single, unavoidable place every piece of raw model output must pass through before
anything downstream ever sees a ``DeploymentPlan`` instance. A model producing something that is
not exactly this shape gets a named validation failure, never a coerced approximation of what it
meant.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from groundwork_contracts.errors import PlanValidationError
from groundwork_contracts.plan import DeploymentPlan


def validate_model_output(raw: Any) -> DeploymentPlan:
    """Validate raw model output against the ``DeploymentPlan`` schema.

    Args:
        raw: Whatever the model produced — parsed JSON, a dict, or anything
            :meth:`DeploymentPlan.model_validate` accepts. Not required to already be a mapping:
            malformed, truncated, or non-mapping output is exactly the case this boundary exists to
            reject cleanly rather than let raise an unhandled exception downstream.

    Returns:
        A fully valid, schema-conformant :class:`DeploymentPlan`.

    Raises:
        PlanValidationError: If ``raw`` fails validation for any reason — missing required field,
            unexpected extra field, wrong type, or executable-content injection caught by
            ``PlanResource``'s own validators. The error carries every individual validation
            failure, not just the first, so a caller can report all of them at once rather than
            making the requester fix one field at a time. This error is intentionally terminal for
            the request: FR-011/T046 require schema-invalid model output to fail immediately, with
            no retry, no coercion, and no schema loosening. Only transient model-call failures may
            retry at the planning layer, because they say nothing about the validity of the model's
            output shape.
    """
    try:
        return DeploymentPlan.model_validate(raw)
    except ValidationError as exc:
        errors = [
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        ]
        raise PlanValidationError(
            "model output failed DeploymentPlan schema validation; the request fails rather than "
            "coercing or retrying with a loosened schema (FR-011)",
            errors=errors,
        ) from exc
