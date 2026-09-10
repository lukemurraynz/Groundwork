"""Health probes — T021 support, FR-042.

Lives in ``groundwork_shared``, not ``groundwork_controlplane.api`` where it started: the
orchestrator worker (``groundwork_orchestrator.worker``) needs the identical registered-check
pattern for its own ``/health/live``/``/health/ready`` routes, and
``test_import_boundaries.py::test_orchestrator_does_not_import_the_control_plane`` forbids it
reaching into the control plane's package to get it. This module has no control-plane-specific
logic — moving it once was correct; duplicating it would have created two copies free to drift.

FR-042 requires readiness to reflect **real dependency health**, and says a pod must not report
ready while a required dependency is unavailable. A probe that returns 200 unconditionally is
named as a defect, so this module is built around registered checks rather than a hardcoded
response.

Both synchronous checks (configuration, the blueprint catalogue) and asynchronous ones (Cosmos,
Key Vault — anything that makes a real network call) are supported, registered through
:meth:`HealthRegistry.register` and :meth:`HealthRegistry.register_async` respectively. Whichever
is used, :meth:`HealthRegistry.evaluate` reports exactly what it verified and never infers health
from silence.

Liveness and readiness are deliberately different questions. Liveness asks "is this process
wedged" — restarting helps. Readiness asks "can this process serve traffic right now" — restarting
does not help if Cosmos is down, and cycling pods during a dependency outage makes it worse.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum


class CheckStatus(StrEnum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: CheckStatus
    detail: str

    @property
    def healthy(self) -> bool:
        return self.status is CheckStatus.HEALTHY


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    """Aggregate readiness.

    ``ready`` is derived, never assigned. A field that could be set independently of the checks
    would eventually be set incorrectly.
    """

    checks: tuple[CheckResult, ...]

    @property
    def ready(self) -> bool:
        # Empty means nothing was verified. That is not-ready, not ready-by-default — the
        # difference matters during startup, when an unregistered check set would otherwise
        # admit traffic before any dependency was confirmed.
        return bool(self.checks) and all(c.healthy for c in self.checks)

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(c for c in self.checks if not c.healthy)


class HealthRegistry:
    """Registered dependency checks.

    Each check is a callable returning :class:`CheckResult`. Registration is explicit so the set of
    verified dependencies is visible in one place rather than scattered across modules that each
    assume someone else is checking.
    """

    def __init__(self) -> None:
        self._checks: dict[str, Callable[[], CheckResult]] = {}
        self._async_checks: dict[str, Callable[[], Awaitable[CheckResult]]] = {}

    def _assert_name_available(self, name: str) -> None:
        if name in self._checks or name in self._async_checks:
            raise ValueError(f"health check {name!r} is already registered")

    def register(self, name: str, check: Callable[[], CheckResult]) -> None:
        self._assert_name_available(name)
        self._checks[name] = check

    def register_async(self, name: str, check: Callable[[], Awaitable[CheckResult]]) -> None:
        """Register a check that performs real I/O — a Cosmos or Key Vault ping, for example.

        Separate from :meth:`register` rather than accepting either shape through one method: the
        two need different execution strategies (call vs. await), and keeping them distinct means
        a caller can't accidentally pass a coroutine function to the sync path and get a
        CheckResult that is actually an un-awaited coroutine object.
        """
        self._assert_name_available(name)
        self._async_checks[name] = check

    @property
    def registered_names(self) -> tuple[str, ...]:
        return tuple(sorted({*self._checks, *self._async_checks}))

    async def evaluate(self) -> ReadinessReport:
        """Run every registered check, sync and async alike.

        A check that raises is a failure, not an omission. Swallowing the exception and reporting
        ready would be precisely the silent-degradation FR-034 prohibits. Async checks run
        concurrently — a slow Cosmos call must not serially delay the Key Vault check behind it.
        """
        results: dict[str, CheckResult] = {}

        for name in sorted(self._checks):
            try:
                results[name] = self._checks[name]()
            except Exception as exc:
                results[name] = CheckResult(
                    name=name,
                    status=CheckStatus.UNHEALTHY,
                    detail=f"check raised {type(exc).__name__}",
                )

        async def _run_async(name: str) -> tuple[str, CheckResult]:
            try:
                return name, await self._async_checks[name]()
            except Exception as exc:
                return name, CheckResult(
                    name=name,
                    status=CheckStatus.UNHEALTHY,
                    detail=f"check raised {type(exc).__name__}",
                )

        if self._async_checks:
            for name, result in await asyncio.gather(
                *(_run_async(name) for name in sorted(self._async_checks))
            ):
                results[name] = result

        return ReadinessReport(checks=tuple(results[name] for name in sorted(results)))


def liveness() -> bool:
    """Whether the process itself is functioning.

    Always True while the interpreter can execute this. Deliberately does not consult dependencies:
    a Cosmos outage must not cause Kubernetes to restart every pod, which would add a thundering
    herd to an existing incident without fixing anything.
    """
    return True
