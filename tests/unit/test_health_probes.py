"""Health probe behaviour — FR-042.

FR-042 says a pod must not report ready while a required dependency is unavailable, and names an
unconditionally-200 probe as a defect. These tests assert the probe fails closed in every direction:
before startup, on a failing dependency, and on a check that raises — for both the synchronous
checks (configuration, blueprint catalogue) and the asynchronous ones that make a real network call
(Cosmos, Key Vault).

The liveness/readiness distinction is tested too, because conflating them causes real operational
harm — restarting every pod during a Cosmos outage adds a thundering herd to an existing incident.
"""

from __future__ import annotations

import pytest

from groundwork_shared.health import (
    CheckResult,
    CheckStatus,
    HealthRegistry,
    ReadinessReport,
    liveness,
)


def _passing(name: str) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.HEALTHY, detail="reachable")


def _failing(name: str) -> CheckResult:
    return CheckResult(name=name, status=CheckStatus.UNHEALTHY, detail="unreachable")


async def _passing_async(name: str) -> CheckResult:
    return _passing(name)


async def test_empty_registry_is_not_ready() -> None:
    """Nothing verified means not ready — never ready-by-default.

    This matters during startup: an unregistered check set would otherwise admit traffic before any
    dependency was confirmed.
    """
    assert (await HealthRegistry().evaluate()).ready is False


async def test_all_passing_checks_is_ready() -> None:
    registry = HealthRegistry()
    registry.register("cosmos", lambda: _passing("cosmos"))
    registry.register("config", lambda: _passing("config"))

    assert (await registry.evaluate()).ready is True


async def test_one_failing_dependency_blocks_readiness() -> None:
    """FR-042 — a single unavailable dependency makes the pod not ready."""
    registry = HealthRegistry()
    registry.register("config", lambda: _passing("config"))
    registry.register("cosmos", lambda: _failing("cosmos"))

    report = await registry.evaluate()

    assert report.ready is False
    assert [c.name for c in report.failures] == ["cosmos"]


async def test_check_that_raises_is_a_failure_not_an_omission() -> None:
    """Swallowing the exception and reporting ready would be exactly the silent degradation
    FR-034 prohibits."""
    registry = HealthRegistry()
    registry.register("config", lambda: _passing("config"))
    registry.register("cosmos", lambda: (_ for _ in ()).throw(TimeoutError("connection timed out")))

    report = await registry.evaluate()

    assert report.ready is False
    failure = next(c for c in report.failures if c.name == "cosmos")
    assert "TimeoutError" in failure.detail


async def test_raising_check_does_not_leak_exception_content() -> None:
    """A dependency error may embed a connection string; only the type is reported (FR-049)."""
    registry = HealthRegistry()
    registry.register(
        "cosmos",
        lambda: (_ for _ in ()).throw(
            RuntimeError("AccountKey=Zm9vYmFyYmF6cXV4;Endpoint=https://x")
        ),
    )

    report = await registry.evaluate()
    detail = report.checks[0].detail

    assert "AccountKey" not in detail
    assert "Zm9vYmFy" not in detail


async def test_duplicate_registration_is_rejected() -> None:
    """Two checks under one name means one silently shadows the other."""
    registry = HealthRegistry()
    registry.register("cosmos", lambda: _passing("cosmos"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register("cosmos", lambda: _passing("cosmos"))


async def test_sync_and_async_names_share_the_same_namespace() -> None:
    """A sync check named 'cosmos' must not coexist with an async check of the same name."""
    registry = HealthRegistry()
    registry.register("cosmos", lambda: _passing("cosmos"))

    with pytest.raises(ValueError, match="already registered"):
        registry.register_async("cosmos", lambda: _passing_async("cosmos"))


async def test_checks_run_in_deterministic_order() -> None:
    """Stable ordering keeps probe output diffable across scrapes."""
    registry = HealthRegistry()
    for name in ("zulu", "alpha", "mike"):
        registry.register(name, lambda n=name: _passing(n))  # type: ignore[misc]

    report = await registry.evaluate()

    assert [c.name for c in report.checks] == ["alpha", "mike", "zulu"]
    assert registry.registered_names == ("alpha", "mike", "zulu")


async def test_async_check_that_succeeds_is_healthy() -> None:
    """The real shape of a Cosmos/Key Vault ping: an awaited call, not a bare return."""
    registry = HealthRegistry()
    registry.register_async("cosmos", lambda: _passing_async("cosmos"))

    report = await registry.evaluate()

    assert report.ready is True
    assert report.checks[0].name == "cosmos"


async def test_async_check_that_raises_is_a_failure() -> None:
    async def _boom() -> CheckResult:
        raise TimeoutError("cosmos did not respond")

    registry = HealthRegistry()
    registry.register("config", lambda: _passing("config"))
    registry.register_async("cosmos", _boom)

    report = await registry.evaluate()

    assert report.ready is False
    failure = next(c for c in report.failures if c.name == "cosmos")
    assert "TimeoutError" in failure.detail


async def test_sync_and_async_checks_combine_in_one_report() -> None:
    """Configuration (sync) and Cosmos (async) must both appear in one readiness report."""
    registry = HealthRegistry()
    registry.register("config", lambda: _passing("config"))
    registry.register_async("cosmos", lambda: _passing_async("cosmos"))
    registry.register_async("key-vault", lambda: _passing_async("key-vault"))

    report = await registry.evaluate()

    assert report.ready is True
    assert [c.name for c in report.checks] == ["config", "cosmos", "key-vault"]
    assert registry.registered_names == ("config", "cosmos", "key-vault")


def test_liveness_ignores_dependencies() -> None:
    """Liveness asks whether the process is wedged, not whether Cosmos is up.

    Restarting a pod does not fix a dependency outage, and cycling every pod during one makes the
    incident worse.
    """
    assert liveness() is True


def test_readiness_report_is_immutable() -> None:
    report = ReadinessReport(checks=(_passing("config"),))

    with pytest.raises(AttributeError):
        report.checks = ()  # type: ignore[misc]
