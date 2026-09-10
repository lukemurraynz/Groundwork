"""Readiness check functions — one module per Azure Landing Zone design area (T036-T042).

Each function matches ``groundwork_controlplane.validation.engine.CheckFunction``: it receives a
:class:`~groundwork_controlplane.validation.engine.ValidationContext` and returns the outcome of
evaluating exactly one assertion. A check returns ``FAILED`` for a genuine "the target environment
is not ready" finding; it raises only when the check itself could not run, which the engine turns
into ``UNREACHABLE`` (FR-015) — a check must not swallow its own connectivity failure and report a
false ``FAILED`` or ``PASSED``.
"""
