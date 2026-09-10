"""T054a re-scoped — conversational/frontend parity with REST (FR-033, SC-025).

Teams left Release 1 on 2026-08-26. The surviving guarantee is unchanged in substance: every
capability reachable through the conversational frontend surfaces must also be reachable through
the REST API, and no mutating capability may exist only behind `/v1/voice/*`.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from collections.abc import Set as AbstractSet
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute

from groundwork_controlplane.api.main import app
from groundwork_controlplane.api.voice import (
    _CHECK_PLAN_STATUS_TOOL_VOICE_LIVE,
    _CONFIRM_CUSTOMER_CONSENT_TOOL_VOICE_LIVE,
    _CREATE_TENANT_TOOL_VOICE_LIVE,
    _GENERATE_PLAN_TOOL_VOICE_LIVE,
    _GET_ONBOARDING_STATUS_TOOL_VOICE_LIVE,
    _GRANT_ADO_ORG_ACCESS_TOOL_VOICE_LIVE,
    _TRIGGER_BOOTSTRAP_IDENTITY_TOOL_VOICE_LIVE,
)

pytestmark = pytest.mark.accessibility

type RouteKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class VoiceParityCase:
    name: str
    voice_route: RouteKey
    rest_routes: tuple[RouteKey, ...]
    justification: str


VOICE_MUTATION_PARITY: Final[tuple[VoiceParityCase, ...]] = (
    # /chat is just the multi-turn conversational transport for the same plan/onboarding facts.
    VoiceParityCase(
        name="voice chat transport",
        voice_route=("POST", "/v1/voice/chat"),
        rest_routes=(
            ("POST", "/v1/plans"),
            ("GET", "/v1/tenants/{tenant_id}/onboarding/lighthouse"),
        ),
        justification=(
            "The chat endpoint exposes only the generate-plan and get-onboarding-status "
            "capabilities, so its non-voice twins are the REST plan and onboarding routes."
        ),
    ),
    # The one-shot transcript bridge is conversational sugar over the same plan-creation contract.
    VoiceParityCase(
        name="voice plan creation",
        voice_route=("POST", "/v1/voice/plan"),
        rest_routes=(("POST", "/v1/plans"),),
        justification=(
            "A voice transcript may create a plan, but FR-033 requires the same "
            "plan-creation capability over REST."
        ),
    ),
    # Voice approval is the conversational wrapper around the same approval + admission flow.
    VoiceParityCase(
        name="voice approval and deployment admission",
        voice_route=("POST", "/v1/voice/approve"),
        rest_routes=(("POST", "/v1/plans/{plan_id}/approvals"), ("POST", "/v1/deployments")),
        justification=(
            "The voice route records approval and queues deployment; the REST flow does those "
            "as two explicit calls."
        ),
    ),
    VoiceParityCase(
        name="voice offshore inference consent",
        voice_route=("POST", "/v1/voice/consent"),
        rest_routes=(("POST", "/v1/tenants/offshore-inference-consent"),),
        justification=(
            "Offshore inference consent is now writable through a tenant-bound REST twin using "
            "the same consent artefact store and disclosure-version contract as the voice route."
        ),
    ),
)

TOOL_ROUTE_PARITY: Final[dict[str, tuple[RouteKey, ...]]] = {
    "generate_plan": (("POST", "/v1/plans"), ("POST", "/v1/voice/plan")),
    "get_onboarding_status": (("GET", "/v1/tenants/{tenant_id}/onboarding/lighthouse"),),
    "create_tenant": (("POST", "/v1/tenants"),),
    "confirm_customer_consent": (("POST", "/v1/tenants/{tenant_id}/onboarding/confirm"),),
    "grant_ado_org_access": (("POST", "/v1/tenants/{tenant_id}/onboarding/ado-access-grant"),),
    "check_plan_status": (("GET", "/v1/plans/{plan_id}"),),
    "trigger_bootstrap_identity": (
        ("POST", "/v1/tenants/{tenant_id}/subscriptions/{subscription_id}/bootstrap-identity"),
    ),
}

VOICE_HTML = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "groundwork_controlplane"
    / "static"
    / "voice.html"
)
VOICE_HTML_PATH_RE = re.compile(r"['\"](/v1/[A-Za-z0-9_./-]*)['\"]")


def _http_route_inventory() -> set[RouteKey]:
    inventory: set[RouteKey] = set()
    for route in _iter_registered_routes():
        if isinstance(route, APIRoute):
            methods = route.methods
            if not isinstance(methods, AbstractSet):
                continue
            for method in methods - {"HEAD", "OPTIONS"}:
                inventory.add((method, route.path))
    return inventory


def _websocket_route_inventory() -> set[str]:
    return {
        route.path for route in _iter_registered_routes() if isinstance(route, APIWebSocketRoute)
    }


def _iter_registered_routes() -> list[object]:
    routes: list[object] = []
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            routes.extend(original_router.routes)
            continue
        routes.append(route)
    return routes


def _tool_name(definition: Mapping[str, object]) -> str:
    nested = definition.get("function")
    if isinstance(nested, Mapping):
        return str(nested["name"])
    return str(definition["name"])


def _resolve_frontend_path(reference: str) -> str:
    all_paths = {path for _method, path in _http_route_inventory()} | _websocket_route_inventory()
    if reference in all_paths:
        return reference
    matches = [
        path for path in all_paths if path.startswith(reference) and "{" in path[len(reference) :]
    ]
    assert len(matches) == 1, f"voice.html references unknown API path fragment {reference!r}"
    return matches[0]


def test_conversational_tools_match_between_transports_and_have_rest_routes() -> None:
    """``/chat`` and ``/ws/voice`` share these exact tool-definition objects (see
    ``api/voice.py``'s ``_*_TOOL_VOICE_LIVE`` constants) — they cannot drift apart the way two
    independently-defined tool sets could, so this only needs to check the one shared set against
    ``TOOL_ROUTE_PARITY`` and the registered REST routes."""
    tools = frozenset(
        {
            _tool_name(_GENERATE_PLAN_TOOL_VOICE_LIVE),
            _tool_name(_GET_ONBOARDING_STATUS_TOOL_VOICE_LIVE),
            _tool_name(_CREATE_TENANT_TOOL_VOICE_LIVE),
            _tool_name(_CONFIRM_CUSTOMER_CONSENT_TOOL_VOICE_LIVE),
            _tool_name(_GRANT_ADO_ORG_ACCESS_TOOL_VOICE_LIVE),
            _tool_name(_CHECK_PLAN_STATUS_TOOL_VOICE_LIVE),
            _tool_name(_TRIGGER_BOOTSTRAP_IDENTITY_TOOL_VOICE_LIVE),
        }
    )

    assert tools == frozenset(TOOL_ROUTE_PARITY), (
        "every conversational tool must have an entry in TOOL_ROUTE_PARITY, and vice versa, so a "
        "new capability cannot bypass this file's review"
    )

    routes = _http_route_inventory()
    for capability, required_routes in TOOL_ROUTE_PARITY.items():
        for route in required_routes:
            assert route in routes, (
                f"tool {capability!r} is exposed conversationally but its REST parity route "
                f"{route} "
                "is missing from the registered FastAPI app"
            )


def test_voice_mutation_pairing_table_covers_every_registered_voice_write_route() -> None:
    voice_write_routes = {
        route
        for route in _http_route_inventory()
        if route[1].startswith("/v1/voice/") and route[0] in {"POST", "PUT", "PATCH", "DELETE"}
    }
    mapped_routes = {case.voice_route for case in VOICE_MUTATION_PARITY}
    assert voice_write_routes == mapped_routes, (
        "every registered /v1/voice/* mutating route must be accounted for in the explicit parity "
        "table so new conversational capabilities cannot bypass review"
    )


@pytest.mark.parametrize("case", VOICE_MUTATION_PARITY, ids=lambda case: case.name)
def test_every_voice_mutation_has_non_voice_rest_twin(case: VoiceParityCase) -> None:
    routes = _http_route_inventory()
    assert case.voice_route in routes, (
        f"expected conversational route {case.voice_route} to be registered"
    )
    for rest_route in case.rest_routes:
        assert rest_route in routes, (
            f"missing REST twin {rest_route} for {case.voice_route}: {case.justification}"
        )
        assert not rest_route[1].startswith("/v1/voice/"), (
            f"REST twin for {case.voice_route} must be non-voice, got {rest_route}: "
            f"{case.justification}"
        )


def test_voice_html_references_only_registered_api_paths() -> None:
    referenced = set(VOICE_HTML_PATH_RE.findall(VOICE_HTML.read_text(encoding="utf-8")))
    resolved = {_resolve_frontend_path(path) for path in referenced}
    registered_paths = {
        path for _method, path in _http_route_inventory()
    } | _websocket_route_inventory()
    assert resolved <= registered_paths, (
        "voice.html references an API path that the FastAPI app does not serve"
    )
