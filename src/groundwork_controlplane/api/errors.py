"""RFC 9457 ``application/problem+json`` error handling (T051).

``control-plane-api.md`` fixes the error shape for every non-2xx response: ``type``, ``title``,
``status``, ``detail``, ``instance``, ``correlationId``, and — for a validation failure — an array
of ``failedAssertions``. This module is the single place that shape is assembled, so every route
raises a plain domain exception and never hand-builds a problem body itself (the same reasoning as
``groundwork_controlplane.agents.boundary`` being the one schema boundary: one seam, not one per
call site, is what keeps the shape actually consistent).

``https://groundwork.invalid/...`` for ``type``, not ``https://groundwork.dev/...`` as
``control-plane-api.md``'s own illustrative example shows: the standing constraint is that
no ``groundwork.*`` domain is owned, so no HTTPS URL claiming one may appear anywhere live,
including an error response a caller could plausibly dereference. ``.invalid`` is the
IANA-reserved TLD for exactly this (RFC 2606) — it cannot resolve to anyone, ours or otherwise —
and matches the two problem types ``api/main.py`` already emits for configuration and
blueprint-catalogue failures.

FR-049: no problem body here ever includes a token, secret, or the caller-unentitled content behind
a failure. ``scrub_text`` runs over anything derived from an exception message before it reaches a
response, the same discipline already applied to log lines and health-check details.
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from groundwork_contracts.errors import PlanIntegrityError, PlanValidationError
from groundwork_controlplane.agents.planning import PlanGenerationError
from groundwork_controlplane.api.auth import AuthenticationError, AuthorizationError
from groundwork_controlplane.approval.service import (
    CostAcknowledgementMismatchError,
    DuplicateApprovalError,
    LicensingDisclosureNotAcknowledgedError,
    NonDurableApprovalChannelError,
    PlanExpiredError,
    PlanHashMismatchError,
    StepUpAuthenticationRequiredError,
)
from groundwork_shared.telemetry.correlation import current_correlation_id
from groundwork_shared.telemetry.scrubbing import scrub_text

logger = logging.getLogger(__name__)

PROBLEM_BASE = "https://groundwork.invalid/problems"


class FailedAssertion(BaseModel):
    model_config = ConfigDict(frozen=True)

    assertion_id: str = Field(serialization_alias="assertionId")
    design_area: str = Field(serialization_alias="designArea")
    finding: str
    remediation: str | None = None


class PlanNotDeployableError(Exception):
    """A plan's readiness re-check (FR-015) found at least one blocking failure.

    Carries only the blocking results — an error response reports what blocks, not the full
    passed-and-failed result set, which is what the caller already gets from the 200 path.
    """

    def __init__(self, failed_assertions: tuple[FailedAssertion, ...]) -> None:
        self.failed_assertions = failed_assertions
        super().__init__(f"{len(failed_assertions)} blocking readiness assertion(s) failed")


class TenantNotFoundError(Exception):
    def __init__(self) -> None:
        super().__init__("tenant not found")


class LighthouseOnboardingNotConfiguredError(Exception):
    def __init__(self) -> None:
        super().__init__(
            "Lighthouse onboarding is built but not configured. The control-plane ARM credential "
            "must be available so Groundwork can resolve its own tenant and principal IDs."
        )


class ReadinessReportNotFoundError(Exception):
    def __init__(self, detail: str = "readiness report not yet generated") -> None:
        super().__init__(detail)


def _problem(
    request: Request,
    *,
    status: int,
    problem_type: str,
    title: str,
    detail: str,
    extra: dict[str, object] | None = None,
) -> JSONResponse:
    body: dict[str, object] = {
        "type": f"{PROBLEM_BASE}/{problem_type}",
        "title": title,
        "status": status,
        "detail": scrub_text(detail),
        "instance": request.url.path,
        "correlationId": current_correlation_id(),
    }
    if extra:
        body.update(extra)
    return JSONResponse(status_code=status, content=body, media_type="application/problem+json")


def register_error_handlers(app: FastAPI) -> None:
    """Attach every domain exception's mapping to an RFC 9457 body.

    Called once from ``main.py``. Each handler maps exactly one exception type — there is no
    catch-all branch inside any handler, so which status code a given failure produces is decided
    by which exception was raised, not by string-matching a message.
    """

    @app.exception_handler(AuthenticationError)
    async def _authentication_error(request: Request, exc: AuthenticationError) -> JSONResponse:
        return _problem(
            request,
            status=401,
            problem_type="authentication",
            title="Authentication failed",
            detail=exc.reason,
        )

    @app.exception_handler(AuthorizationError)
    async def _authorization_error(request: Request, exc: AuthorizationError) -> JSONResponse:
        return _problem(
            request,
            status=403,
            problem_type="authorization",
            title="Caller is not entitled to perform this operation",
            detail=exc.detail,
        )

    @app.exception_handler(PlanValidationError)
    async def _plan_validation_error(request: Request, exc: PlanValidationError) -> JSONResponse:
        # FR-011: the planning agent's output failed schema validation. Surfaced, not retried
        # against a loosened schema.
        return _problem(
            request,
            status=422,
            problem_type="plan-schema-validation",
            title="Plan is not deployable",
            detail=exc.detail,
            extra={"validationErrors": exc.errors},
        )

    @app.exception_handler(PlanIntegrityError)
    async def _plan_integrity_error(request: Request, exc: PlanIntegrityError) -> JSONResponse:
        # The plan changed after it was hashed; any bound approval is void (FR-022).
        return _problem(
            request,
            status=409,
            problem_type="plan-integrity",
            title="Plan integrity check failed",
            detail=str(exc),
        )

    @app.exception_handler(PlanHashMismatchError)
    async def _plan_hash_mismatch_error(
        request: Request, exc: PlanHashMismatchError
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="approval-plan-hash-mismatch",
            title="Submitted plan hash does not match the current plan",
            detail=str(exc),
        )

    @app.exception_handler(PlanExpiredError)
    async def _plan_expired_error(request: Request, exc: PlanExpiredError) -> JSONResponse:
        return _problem(
            request,
            status=410,
            problem_type="plan-expired",
            title="Plan validity window has expired",
            detail=str(exc),
        )

    @app.exception_handler(NonDurableApprovalChannelError)
    async def _non_durable_channel_error(
        request: Request, exc: NonDurableApprovalChannelError
    ) -> JSONResponse:
        return _problem(
            request,
            status=422,
            problem_type="non-durable-approval-channel",
            title="Approval channel does not yield a durable artefact",
            detail=str(exc),
        )

    @app.exception_handler(CostAcknowledgementMismatchError)
    async def _cost_mismatch_error(
        request: Request, exc: CostAcknowledgementMismatchError
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="cost-acknowledgement-mismatch",
            title="Acknowledged cost does not match the current estimate",
            detail=str(exc),
        )

    @app.exception_handler(LicensingDisclosureNotAcknowledgedError)
    async def _licensing_disclosure_not_acknowledged(
        request: Request, exc: LicensingDisclosureNotAcknowledgedError
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="licensing-disclosure-not-acknowledged",
            title="Power BI viewer-licensing disclosure must be acknowledged",
            detail=str(exc),
        )

    @app.exception_handler(DuplicateApprovalError)
    async def _duplicate_approval_error(
        request: Request, exc: DuplicateApprovalError
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="duplicate-approval",
            title="Approval already recorded",
            detail=str(exc),
        )

    @app.exception_handler(StepUpAuthenticationRequiredError)
    async def _step_up_authentication_required(
        request: Request, exc: StepUpAuthenticationRequiredError
    ) -> JSONResponse:
        return _problem(
            request,
            status=403,
            problem_type="step-up-authentication-required",
            title="Step-up authentication is required",
            detail=str(exc),
        )

    @app.exception_handler(PlanGenerationError)
    async def _plan_generation_error(request: Request, exc: PlanGenerationError) -> JSONResponse:
        # The agent call itself did not complete usably (empty or non-JSON response) — distinct
        # from a schema failure, this is the planning agent behaving as an unavailable dependency.
        return _problem(
            request,
            status=502,
            problem_type="planning-agent-unavailable",
            title="The planning agent could not produce a plan",
            detail=str(exc),
        )

    @app.exception_handler(PlanNotDeployableError)
    async def _plan_not_deployable_error(
        request: Request, exc: PlanNotDeployableError
    ) -> JSONResponse:
        return _problem(
            request,
            status=409,
            problem_type="validation-blocking",
            title="Plan is not deployable",
            detail=f"{len(exc.failed_assertions)} blocking readiness assertion(s) failed.",
            extra={
                "failedAssertions": [
                    assertion.model_dump(by_alias=True) for assertion in exc.failed_assertions
                ]
            },
        )

    @app.exception_handler(TenantNotFoundError)
    async def _tenant_not_found_error(request: Request, exc: TenantNotFoundError) -> JSONResponse:
        return _problem(
            request,
            status=404,
            problem_type="tenant-not-found",
            title="Tenant not found",
            detail=str(exc),
        )

    @app.exception_handler(LighthouseOnboardingNotConfiguredError)
    async def _lighthouse_onboarding_not_configured_error(
        request: Request, exc: LighthouseOnboardingNotConfiguredError
    ) -> JSONResponse:
        return _problem(
            request,
            status=503,
            problem_type="lighthouse-onboarding-not-configured",
            title="Lighthouse onboarding is not configured",
            detail=str(exc),
        )

    @app.exception_handler(ReadinessReportNotFoundError)
    async def _readiness_report_not_found_error(
        request: Request, exc: ReadinessReportNotFoundError
    ) -> JSONResponse:
        return _problem(
            request,
            status=404,
            problem_type="readiness-report-not-found",
            title="Readiness report not found",
            detail=str(exc),
        )

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return _problem(
            request,
            status=400,
            problem_type="malformed-request",
            title="Request body is malformed",
            detail="; ".join(
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors()
            ),
        )

    @app.exception_handler(Exception)
    async def _unhandled_error(request: Request, exc: Exception) -> JSONResponse:
        # Last resort. Logged with a stack trace for operators; the response itself carries no
        # exception detail beyond the type name, per FR-049 — an unhandled failure must not leak
        # internals to the caller just because nobody wrote a specific handler for it yet.
        logger.error("unhandled exception", exc_info=True)
        return _problem(
            request,
            status=500,
            problem_type="internal-error",
            title="An unexpected error occurred",
            detail=f"{type(exc).__name__} was not handled by a specific error mapping.",
        )
