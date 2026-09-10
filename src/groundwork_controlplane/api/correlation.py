"""Correlation-ID HTTP middleware (T019, FR-048).

The FastAPI/Starlette-specific half of correlation propagation. The context var, log filter, and
``CorrelationScope`` this middleware sets live in ``groundwork_shared.telemetry.correlation``
because the orchestrator needs them too and has no FastAPI dependency to import them alongside —
see that module's docstring for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from groundwork_shared.telemetry.correlation import (
    CORRELATION_HEADER,
    GUID_PATTERN,
    CorrelationScope,
    new_correlation_id,
)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Honours an inbound ``X-Correlation-ID`` if well-formed, mints one otherwise.

    Conversation is untrusted input, but a correlation ID is not an authority claim:
    it cannot select a tenant, change what is validated, or authorise anything. It is checked
    against the GUID pattern and nothing more; a malformed one is silently replaced rather than
    rejected, because refusing a request over one cosmetic header is a worse failure mode than
    minting a fresh ID.

    Always echoes the resolved ID back on the response, whether it came from the caller or was
    freshly minted — a caller that did not send one still learns which ID its request was logged
    under, which is what makes a later support query ("what happened with request X") answerable.
    """

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get(CORRELATION_HEADER)
        correlation_id = (
            incoming if incoming and GUID_PATTERN.match(incoming) else new_correlation_id()
        )

        with CorrelationScope(correlation_id):
            response = await call_next(request)

        response.headers[CORRELATION_HEADER] = correlation_id
        return response
