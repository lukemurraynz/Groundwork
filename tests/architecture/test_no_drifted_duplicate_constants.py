"""Guards against the exact bug found live 2026-09-07: two modules independently declared a
constant named ``CONNECTION_DATA_API_VERSION`` for the same Azure DevOps ``connectionData`` call.
``groundwork_orchestrator.stages.identity`` had the live-verified correct value ("7.1-preview");
``groundwork_shared.validation.checks.devops`` had a stale "7.1" that 400s on every real
organization, silently breaking readiness checks and conversation-time engagement-detail
persistence with no test catching it, because nothing ever compared the two.
"""

from __future__ import annotations

from groundwork_orchestrator.stages.identity import (
    CONNECTION_DATA_API_VERSION as IDENTITY_CONNECTION_DATA_API_VERSION,
)
from groundwork_shared.validation.checks.devops import (
    CONNECTION_DATA_API_VERSION as DEVOPS_CHECKS_CONNECTION_DATA_API_VERSION,
)


def test_connection_data_api_version_is_not_drifted_between_modules() -> None:
    assert (
        DEVOPS_CHECKS_CONNECTION_DATA_API_VERSION == IDENTITY_CONNECTION_DATA_API_VERSION
    ), (
        "groundwork_shared.validation.checks.devops.CONNECTION_DATA_API_VERSION has drifted from "
        "groundwork_orchestrator.stages.identity.CONNECTION_DATA_API_VERSION — both call the same "
        "Azure DevOps connectionData endpoint and must agree; the live-verified value lives in "
        "identity.py's docstring."
    )
