"""T051 — RFC 9457 ``application/problem+json`` error handling.

Each test raises exactly the exception a route would raise and asserts the resulting body against
``control-plane-api.md``'s fixed error shape, rather than exercising a real route — the mapping
itself is what this module owns; ``test_plans_endpoint.py`` proves it fires from real routes.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from groundwork_contracts.errors import PlanIntegrityError, PlanValidationError
from groundwork_controlplane.agents.planning import PlanGenerationError
from groundwork_controlplane.api.auth import AuthenticationError, AuthorizationError
from groundwork_controlplane.api.errors import (
    FailedAssertion,
    PlanNotDeployableError,
    register_error_handlers,
)


def _app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/raise/authentication")
    async def _authentication() -> None:
        raise AuthenticationError("token has expired")

    @app.get("/raise/authorization")
    async def _authorization() -> None:
        raise AuthorizationError("caller lacks the required role")

    @app.get("/raise/plan-validation")
    async def _plan_validation() -> None:
        raise PlanValidationError(
            "model output failed validation", errors=["subscription_id: missing"]
        )

    @app.get("/raise/plan-integrity")
    async def _plan_integrity() -> None:
        raise PlanIntegrityError(expected="sha256:aaa", actual="sha256:bbb")

    @app.get("/raise/plan-generation")
    async def _plan_generation() -> None:
        raise PlanGenerationError("planning agent returned an empty response")

    @app.get("/raise/not-deployable")
    async def _not_deployable() -> None:
        raise PlanNotDeployableError(
            (
                FailedAssertion(
                    assertion_id="network.vnet-address-space-available",
                    design_area="network-topology-and-connectivity",
                    finding="10.42.0.0/16 overlaps an existing VNet",
                    remediation="Choose a non-overlapping address range.",
                ),
            )
        )

    @app.get("/raise/unhandled")
    async def _unhandled() -> None:
        raise RuntimeError("something the code did not anticipate")

    return app


def _client() -> TestClient:
    return TestClient(_app(), raise_server_exceptions=False)


def test_authentication_error_maps_to_401() -> None:
    response = _client().get("/raise/authentication")
    assert response.status_code == 401
    body = response.json()
    assert body["status"] == 401
    assert body["type"] == "https://groundwork.invalid/problems/authentication"
    assert body["instance"] == "/raise/authentication"
    assert "correlationId" in body


def test_authorization_error_maps_to_403() -> None:
    response = _client().get("/raise/authorization")
    assert response.status_code == 403
    assert response.json()["type"] == "https://groundwork.invalid/problems/authorization"


def test_plan_validation_error_maps_to_422_with_errors() -> None:
    response = _client().get("/raise/plan-validation")
    assert response.status_code == 422
    body = response.json()
    assert body["validationErrors"] == ["subscription_id: missing"]


def test_plan_integrity_error_maps_to_409() -> None:
    response = _client().get("/raise/plan-integrity")
    assert response.status_code == 409
    assert "integrity" in response.json()["title"].lower()


def test_plan_generation_error_maps_to_502() -> None:
    response = _client().get("/raise/plan-generation")
    assert response.status_code == 502


def test_plan_not_deployable_error_maps_to_409_with_failed_assertions() -> None:
    response = _client().get("/raise/not-deployable")
    assert response.status_code == 409
    failed = response.json()["failedAssertions"]
    assert len(failed) == 1
    assert failed[0]["assertionId"] == "network.vnet-address-space-available"
    assert failed[0]["remediation"] == "Choose a non-overlapping address range."


def test_unhandled_exception_maps_to_500_without_leaking_detail() -> None:
    response = _client().get("/raise/unhandled")
    assert response.status_code == 500
    detail = response.json()["detail"]
    assert "RuntimeError" in detail
    assert "something the code did not anticipate" not in detail


def test_no_problem_body_ever_omits_required_fields() -> None:
    for path in ("/raise/authentication", "/raise/authorization", "/raise/plan-generation"):
        body = _client().get(path).json()
        for field in ("type", "title", "status", "detail", "instance", "correlationId"):
            assert field in body, f"{path} is missing {field!r}"
