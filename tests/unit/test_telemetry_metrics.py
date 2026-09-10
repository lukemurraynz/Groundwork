"""T086a — custom stage-duration and queue-depth metrics (FR-054).

No exporter is configured in tests (``configure_telemetry`` is never called), so these exercise the
OpenTelemetry SDK's own default no-op ``MeterProvider`` — the point of the test is that recording
never raises regardless of whether a real exporter is behind it, the same degraded-state discipline
``otel.py`` already documents for tracing and logging.
"""

from __future__ import annotations

from groundwork_shared.telemetry.metrics import (
    record_drift_blocking_failures,
    record_queue_depth,
    record_stage_duration,
)


def test_record_stage_duration_does_not_raise() -> None:
    record_stage_duration(stage_name="infrastructure", outcome="succeeded", duration_seconds=12.5)


def test_record_queue_depth_does_not_raise() -> None:
    record_queue_depth(tenant_id="11111111-1111-1111-1111-111111111111", depth=3)


def test_record_drift_blocking_failures_does_not_raise() -> None:
    record_drift_blocking_failures(
        tenant_id="11111111-1111-1111-1111-111111111111",
        subscription_id="33333333-3333-3333-3333-333333333333",
        blocking_failures=2,
    )
