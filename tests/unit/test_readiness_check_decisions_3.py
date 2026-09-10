"""Ninth readiness check — pure decision logic for the Fabric service-principal API probe.

Same reasoning as ``test_readiness_check_decisions.py`` and ``_2.py``: the real HTTP call in the
check module is untested here; what's tested is the decision given a hypothetical response.
"""

from __future__ import annotations

from groundwork_contracts.readiness import ValidationStatus
from groundwork_shared.validation.checks.fabric import _evaluate_response


def test_200_response_passes() -> None:
    status, finding = _evaluate_response(200)
    assert status is ValidationStatus.PASSED
    assert "authenticated" in finding


def test_401_response_fails_naming_the_tenant_setting() -> None:
    status, finding = _evaluate_response(401)
    assert status is ValidationStatus.FAILED
    assert "Service principals can call Fabric public APIs" in finding
    assert "security groups" in finding


def test_403_response_fails_naming_the_tenant_setting() -> None:
    status, finding = _evaluate_response(403)
    assert status is ValidationStatus.FAILED
    assert "Service principals can call Fabric public APIs" in finding


def test_unexpected_status_fails() -> None:
    status, finding = _evaluate_response(503)
    assert status is ValidationStatus.FAILED
    assert "503" in finding
