"""Rate limiting on the deliberately pre-auth enablement route.

Unit coverage for `_EnablementRateLimiter`'s sliding-window semantics, plus one contract-level
proof that the route actually enforces it via `app.state.enablement_limiter`.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from groundwork_controlplane.api.voice import _EnablementRateLimiter


class _FakeClock:
    def __init__(self) -> None:
        self._now = datetime(2026, 8, 26, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


def _limiter(
    clock: _FakeClock, *, max_requests: int = 2
) -> tuple[_EnablementRateLimiter, _FakeClock]:
    return (
        _EnablementRateLimiter(max_requests=max_requests, window_seconds=60.0, now_fn=clock),
        clock,
    )


def test_allows_up_to_max_then_raises_429() -> None:
    limiter, _ = _limiter(_FakeClock())
    limiter.check("1.2.3.4")
    limiter.check("1.2.3.4")
    with pytest.raises(HTTPException) as exc:
        limiter.check("1.2.3.4")
    assert exc.value.status_code == 429


def test_window_slide_recovers_allowance() -> None:
    limiter, clock = _limiter(_FakeClock())
    limiter.check("h")
    limiter.check("h")
    with pytest.raises(HTTPException):
        limiter.check("h")
    clock.advance(61.0)
    limiter.check("h")  # oldest hits aged out of the sliding window


def test_hosts_are_isolated() -> None:
    limiter, _ = _limiter(_FakeClock())
    limiter.check("a")
    limiter.check("a")
    limiter.check("b")  # host b unaffected by host a's exhaustion
    with pytest.raises(HTTPException):
        limiter.check("a")


def test_missing_client_host_is_its_own_bucket() -> None:
    limiter, _ = _limiter(_FakeClock())
    limiter.check("unknown")
    limiter.check("unknown")
    with pytest.raises(HTTPException):
        limiter.check("unknown")
    limiter.check("other-host")  # unaffected bucket


# --------------------------------------------------------------------------------------
# Contract: the route must actually consult the limiter configured on app.state.
# --------------------------------------------------------------------------------------


def test_enablement_route_enforces_limiter() -> None:
    """A minimal FastAPI app mounting the real voice router proves the ROUTE (not just the
    class) consults ``app.state.enablement_limiter`` — second call inside the window is 429,
    and it recovers once the window slides."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from groundwork_controlplane.api.voice import router as voice_router

    class _Gate:
        async def check(self, tenant_id: str) -> Any:
            from groundwork_channels.voice.enablement import VoiceEnablementResult

            return VoiceEnablementResult(may_accept=True, reason="ok")

    app = FastAPI()
    app.include_router(voice_router)
    clock = _FakeClock()
    app.state.settings = type("S", (), {"voice_live_endpoint": "wss://example"})()
    app.state.voice_gate = _Gate()
    app.state.enablement_limiter = _EnablementRateLimiter(
        max_requests=1, window_seconds=60.0, now_fn=clock
    )

    client = TestClient(app)
    tenant_id = "00000000-0000-4000-8000-000000000001"

    first = client.get(f"/v1/voice/enablement/{tenant_id}")
    assert first.status_code == 200, first.text
    second = client.get(f"/v1/voice/enablement/{tenant_id}")
    assert second.status_code == 429

    clock.advance(61.0)
    third = client.get(f"/v1/voice/enablement/{tenant_id}")
    assert third.status_code == 200
