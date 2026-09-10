"""T019 — correlation-ID propagation (FR-048).

Split to match the module split: the context-var/log-filter logic in
``groundwork_shared.telemetry.correlation`` is tested directly; the HTTP-specific behaviour in
``groundwork_controlplane.api.correlation.CorrelationIdMiddleware`` is tested against a minimal
throwaway FastAPI app — not the real ``groundwork_controlplane.api.main`` app, which requires a full
set of Azure environment variables to even construct its ``Settings`` at startup.
"""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_controlplane.api.correlation import CorrelationIdMiddleware
from groundwork_shared.telemetry.correlation import (
    CORRELATION_HEADER,
    CorrelationIdLogFilter,
    CorrelationScope,
    current_correlation_id,
)

VALID_ID = "11111111-1111-1111-1111-111111111111"
OTHER_VALID_ID = "22222222-2222-2222-2222-222222222222"


# --- CorrelationScope / context var ------------------------------------------------


def test_no_active_scope_reports_none() -> None:
    assert current_correlation_id() is None


def test_scope_makes_the_id_current_while_active() -> None:
    assert current_correlation_id() is None
    with CorrelationScope(VALID_ID):
        assert current_correlation_id() == VALID_ID
    assert current_correlation_id() is None


def test_nested_scopes_restore_the_outer_id() -> None:
    with CorrelationScope(VALID_ID):
        with CorrelationScope(OTHER_VALID_ID):
            assert current_correlation_id() == OTHER_VALID_ID
        assert current_correlation_id() == VALID_ID


def test_scope_rejects_a_malformed_id() -> None:
    with pytest.raises(ValueError, match="not a well-formed GUID"):
        CorrelationScope("not-a-guid")


def test_scope_resets_even_if_the_body_raises() -> None:
    with pytest.raises(RuntimeError), CorrelationScope(VALID_ID):
        raise RuntimeError("boom")
    assert current_correlation_id() is None


# --- CorrelationIdLogFilter ---------------------------------------------------------


def _record() -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=(),
        exc_info=None,
    )


def test_log_filter_defaults_to_a_placeholder_outside_any_scope() -> None:
    record = _record()
    assert CorrelationIdLogFilter().filter(record) is True
    assert record.correlation_id == "-"  # type: ignore[attr-defined]


def test_log_filter_attaches_the_active_correlation_id() -> None:
    record = _record()
    with CorrelationScope(VALID_ID):
        CorrelationIdLogFilter().filter(record)
    assert record.correlation_id == VALID_ID  # type: ignore[attr-defined]


# --- CorrelationIdMiddleware ---------------------------------------------------------


@pytest.fixture
def app_with_middleware() -> FastAPI:
    app = FastAPI()
    app.add_middleware(CorrelationIdMiddleware)

    @app.get("/probe")
    async def probe() -> dict[str, str | None]:
        return {"seen_inside_handler": current_correlation_id()}

    return app


def test_middleware_mints_an_id_when_none_is_supplied(app_with_middleware: FastAPI) -> None:
    with TestClient(app_with_middleware) as client:
        response = client.get("/probe")

    assert response.status_code == 200
    minted = response.headers[CORRELATION_HEADER]
    assert minted == response.json()["seen_inside_handler"]
    # A fresh UUID4, not echoing back a caller value that was never sent.
    assert minted is not None


def test_middleware_honours_a_well_formed_inbound_id(app_with_middleware: FastAPI) -> None:
    with TestClient(app_with_middleware) as client:
        response = client.get("/probe", headers={CORRELATION_HEADER: VALID_ID})

    assert response.headers[CORRELATION_HEADER] == VALID_ID
    assert response.json()["seen_inside_handler"] == VALID_ID


def test_middleware_replaces_a_malformed_inbound_id(app_with_middleware: FastAPI) -> None:
    with TestClient(app_with_middleware) as client:
        response = client.get("/probe", headers={CORRELATION_HEADER: "not-a-guid"})

    returned = response.headers[CORRELATION_HEADER]
    assert returned != "not-a-guid"
    assert response.json()["seen_inside_handler"] == returned


def test_correlation_id_does_not_leak_across_context_after_the_request(
    app_with_middleware: FastAPI,
) -> None:
    """The middleware must reset its scope, or one request's ID would bleed into the next task
    sharing this worker's context — exactly what FR-048 requires it not do."""
    with TestClient(app_with_middleware) as client:
        client.get("/probe", headers={CORRELATION_HEADER: VALID_ID})

    assert current_correlation_id() is None
