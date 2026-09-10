"""Correlation-ID propagation — the context var and log filter (T019, FR-048).

Every request, log line, and audit record produced while handling one customer-facing operation
must carry the same correlation ID, so an incident investigation can reconstruct the full path end
to end rather than approximating it by timestamp.

A ``contextvars.ContextVar`` carries the value rather than a global or thread-local: FastAPI/uvicorn
handles many requests concurrently on the same event loop, and a thread-local would leak one
request's ID into another's log lines the moment two requests interleave on the same worker.

Deliberately no web-framework import here. This module is shared by the control plane (which has
FastAPI/Starlette and wires :class:`CorrelationScope` to an inbound HTTP request — see
``groundwork_controlplane.api.correlation``) and the orchestrator, which has neither dependency and
uses :class:`CorrelationScope` directly around a queued deployment's own correlation ID. Importing a
Starlette middleware class here would make that import fail in the orchestrator's container, whose
image never installs FastAPI (``docker/orchestrator.Dockerfile`` installs only the ``orchestrator``
and ``telemetry`` extras).
"""

from __future__ import annotations

import logging
import re
import uuid
from contextvars import ContextVar, Token

CORRELATION_HEADER = "X-Correlation-ID"

GUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)

_current: ContextVar[str | None] = ContextVar("groundwork_correlation_id", default=None)


def current_correlation_id() -> str | None:
    """The correlation ID for the request or scope active on this task, or ``None`` outside one."""
    return _current.get()


def new_correlation_id() -> str:
    return str(uuid.uuid4())


class CorrelationScope:
    """Establishes a correlation ID for the duration of a ``with`` block.

    Used directly by the orchestrator around a queued deployment's own correlation ID, and by the
    control plane's HTTP middleware around one request — see module docstring for why the two
    integrations live in different packages while sharing this context var.
    """

    def __init__(self, correlation_id: str) -> None:
        if not GUID_PATTERN.match(correlation_id):
            raise ValueError(f"correlation_id {correlation_id!r} is not a well-formed GUID")
        self._correlation_id = correlation_id
        self._token: Token[str | None] | None = None

    def __enter__(self) -> str:
        self._token = _current.set(self._correlation_id)
        return self._correlation_id

    def __exit__(self, *exc_info: object) -> None:
        if self._token is not None:
            _current.reset(self._token)
            self._token = None


class CorrelationIdLogFilter(logging.Filter):
    """Attaches the active correlation ID to every log record as ``record.correlation_id``.

    Always sets the attribute — including the placeholder ``"-"`` when no scope is active, such as
    during process startup before any request has arrived — because a log formatter referencing
    ``%(correlation_id)s`` raises ``KeyError`` on any record missing the attribute entirely. A log
    line untraceable to a request is an acceptable degraded state; a crashing formatter is not.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = current_correlation_id() or "-"
        return True
