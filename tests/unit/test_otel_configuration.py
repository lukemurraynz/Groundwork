"""T017 — the one deterministic, offline-testable behaviour of ``configure_telemetry``.

Everything downstream of a real connection string is Azure Monitor's own SDK — not re-tested here,
same reasoning as ``tests/security/test_credential_scoping.py``'s fake credential: the real exporter
and credential resolution are Azure's concern. What belongs to this module, and is worth testing, is
that an absent connection string is a genuine no-export no-op rather than a disguised failure or a
stand-in exporter.
"""

from __future__ import annotations

import logging
import sys

from groundwork_shared.telemetry.otel import configure_telemetry


def test_missing_connection_string_configures_no_exporter() -> None:
    """No connection string must not import or touch the Azure Monitor SDK at all."""
    assert "azure.monitor.opentelemetry" not in sys.modules

    configure_telemetry(connection_string=None, service_name="groundwork-test", logger_name="")

    # The lazy import inside configure_telemetry is skipped entirely on this path — proof there is
    # no attempt to reach Azure Monitor when the required configuration is absent.
    assert "azure.monitor.opentelemetry" not in sys.modules


def test_missing_connection_string_still_sets_the_logger_level() -> None:
    logger = logging.getLogger("groundwork-test-level")
    logger.setLevel(logging.WARNING)

    configure_telemetry(
        connection_string=None, service_name="groundwork-test", logger_name="groundwork-test-level"
    )

    assert logger.level == logging.INFO


def test_empty_string_connection_string_is_treated_as_absent() -> None:
    """An empty string is a falsy 'not configured', not a value to hand to the SDK."""
    configure_telemetry(connection_string="", service_name="groundwork-test", logger_name="")

    assert "azure.monitor.opentelemetry" not in sys.modules
