"""Custom OpenTelemetry metrics (T086a; FR-054).

``configure_telemetry`` (``otel.py``) wires the exporter and auto-instruments outgoing HTTP/Azure
SDK calls for free — this module is for the two signals auto-instrumentation cannot produce on its
own: *which orchestration stage* took how long (as opposed to which individual HTTP call did), and
*how deep* a tenant's queue is right now. Recorded via the stable OpenTelemetry metrics API
(``opentelemetry.metrics``), which reads whatever ``MeterProvider`` ``configure_azure_monitor``
already installed globally — nothing here constructs its own provider or exporter, so this degrades
the same way tracing/logging already do when no Application Insights connection string is
configured (the SDK's default no-op provider silently drops recordings rather than raising).

Naming: underscore-separated (``groundwork_stage_duration_seconds``, not
``groundwork.stage.duration_seconds``) to match ``groundwork_queue_depth`` — the metric name
``observability.bicep``'s ``alert-gw-queue-depth`` already queried before either this module or a
real emitter for it existed (found 2026-08-02 while adding the stage-duration alert alongside it:
neither that alert's metric nor ``alert-gw-deployment-failures``'s event were emitted by anything in
this codebase — both alerts would have deployed successfully and never fired. Fixed alongside this
module; see ``Sequencer._mark_halted`` for the ``deployment_failed`` event this module doesn't
own).
"""

from __future__ import annotations

from opentelemetry import metrics

_meter = metrics.get_meter("groundwork")

stage_duration_seconds = _meter.create_histogram(
    name="groundwork_stage_duration_seconds",
    unit="s",
    description=(
        "Wall-clock duration of one blueprint stage attempt, tagged by stage_name and outcome — "
        "the signal alert-gw-stage-duration-breach (observability.bicep, T086a/FR-054) queries."
    ),
)

queue_depth = _meter.create_gauge(
    name="groundwork_queue_depth",
    unit="1",
    description=(
        "Number of queued deployments for one tenant, sampled once per queue-consumption loop "
        "poll cycle — the signal alert-gw-queue-depth (observability.bicep, FR-045a/FR-054) "
        "queries."
    ),
)

drift_blocking_failures = _meter.create_gauge(
    name="groundwork_drift_blocking_failures",
    unit="1",
    description=(
        "Number of blocking readiness failures in the latest drift evaluation for one tenant and "
        "subscription."
    ),
)


def record_stage_duration(*, stage_name: str, outcome: str, duration_seconds: float) -> None:
    stage_duration_seconds.record(
        duration_seconds, attributes={"stage_name": stage_name, "outcome": outcome}
    )


def record_queue_depth(*, tenant_id: str, depth: int) -> None:
    queue_depth.set(depth, attributes={"tenant_id": tenant_id})


def record_drift_blocking_failures(
    *, tenant_id: str, subscription_id: str, blocking_failures: int
) -> None:
    drift_blocking_failures.set(
        blocking_failures,
        attributes={"tenant_id": tenant_id, "subscription_id": subscription_id},
    )
