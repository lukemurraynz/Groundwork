"""OpenTelemetry tracing, metrics, and structured logging, wired to Azure Monitor (T017, FR-048).

This module configures a real exporter, or none at all — there is no console-only or mock tracer
standing in for Azure Monitor on the production path. When
``connection_string`` is absent, telemetry genuinely does not leave the process. That is the
documented degraded state for optional configuration
(``groundwork_shared.config.settings.Settings.applicationinsights_connection_string``: "telemetry
degrades to console logging if absent, which is a real operational state rather than a silent
fallback for a required dependency"), not a stub standing in for a required one.

Ingestion authenticates with the same workload identity every other Azure client in this codebase
uses (the secretless-identity rule), via the ``credential`` keyword ``configure_azure_monitor``
accepts — the
connection string identifies which Application Insights resource to send to;
``DefaultAzureCredential`` is what proves the caller is allowed to write to it. Nothing here relies
on the connection string alone as a credential.
"""

from __future__ import annotations

import logging

from groundwork_shared.telemetry.scrubbing import ScrubbingFilter


def configure_telemetry(
    *,
    connection_string: str | None,
    service_name: str,
    logger_name: str = "",
) -> None:
    """Wire OpenTelemetry tracing, metrics, and logging to Azure Monitor for ``service_name``.

    ``connection_string`` absent leaves the named logger at ``INFO`` with whatever handlers the
    caller has already attached (typically a console handler for local dev and `kubectl logs`) and
    configures no exporter — a documented degraded state, not a fallback that masks a missing
    required dependency.

    Args:
        connection_string: Application Insights connection string, or ``None`` to skip Azure Monitor
            export entirely.
        service_name: OpenTelemetry ``service.name`` resource attribute — how this process's traces
            and logs are distinguished from the other service's in Application Insights (e.g.
            ``"groundwork-controlplane"`` vs ``"groundwork-orchestrator"``).
        logger_name: The logger Azure Monitor's logging integration attaches to, and the one that
            gets the scrubbing filter applied to every handler on it. Defaults to the root logger
            (``""``) so every module's log calls reach it through normal propagation without every
            caller having to know and pass a specific name — this must match whatever logger the
            caller's own handler setup (e.g. ``logging.basicConfig``) actually configures.
    """
    target_logger = logging.getLogger(logger_name)
    target_logger.setLevel(logging.INFO)

    if not connection_string:
        return

    # Imported here, not at module scope: importing azure.monitor.opentelemetry has a real cost
    # (it eagerly imports a wide instrumentation surface), and every caller of this module that
    # only wants the type-checked signature — or that never has a connection string, such as a
    # local dev run — should not pay it. The contracts import-boundary test does not reach this
    # package, so this is a performance and blast-radius choice, not a deterministic-execution one.
    from azure.identity import DefaultAzureCredential
    from azure.monitor.opentelemetry import configure_azure_monitor
    from opentelemetry.sdk.resources import Resource

    configure_azure_monitor(
        connection_string=connection_string,
        credential=DefaultAzureCredential(),
        logger_name=logger_name,
        resource=Resource.create({"service.name": service_name}),
    )

    # FR-049 / SC-013 applies to every output path, including what Azure Monitor exports, not only
    # a console handler a caller attaches separately. configure_azure_monitor above adds its own
    # LoggingHandler to target_logger; without this, a secret-shaped string reaching this logger
    # would be scrubbed on the console but not in Application Insights.
    scrubber = ScrubbingFilter()
    for handler in target_logger.handlers:
        handler.addFilter(scrubber)
