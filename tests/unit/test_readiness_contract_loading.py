"""The shipped assertions manifest must validate against the contract (T034).

Mirrors ``tests/unit/test_blueprint_catalogue.py``: proves the FR-014a readiness contract loaded
from ``infra/blueprints/standard-production-fabric/assertions.yaml`` is real and complete, not just
schema-shaped in isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundwork_contracts.blueprint import DesignAreaName
from groundwork_shared.config.readiness import (
    ReadinessContractLoadError,
    load_landing_zone_contract,
)

MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "infra"
    / "blueprints"
    / "standard-production-fabric"
    / "assertions.yaml"
)


def test_shipped_manifest_loads_and_validates() -> None:
    contract = load_landing_zone_contract(MANIFEST)

    assert contract.contract_version == "1.0.0"
    assert contract.assertions


def test_every_design_area_is_covered() -> None:
    """The contract model itself refuses to load otherwise; this pins the shipped manifest as the
    proof, not just the validator's existence."""
    contract = load_landing_zone_contract(MANIFEST)

    covered = {a.design_area for a in contract.assertions}
    assert covered == set(DesignAreaName)


def test_assertion_ids_are_unique() -> None:
    contract = load_landing_zone_contract(MANIFEST)

    ids = [a.assertion_id for a in contract.assertions]
    assert len(ids) == len(set(ids))


def test_every_assertion_has_a_remediation_long_enough_to_act_on() -> None:
    contract = load_landing_zone_contract(MANIFEST)

    for assertion in contract.assertions:
        assert len(assertion.remediation) >= 15


def test_missing_manifest_fails_loudly(tmp_path: Path) -> None:
    with pytest.raises(ReadinessContractLoadError, match="not found"):
        load_landing_zone_contract(tmp_path / "nope.yaml")


def test_malformed_yaml_fails_loudly(tmp_path: Path) -> None:
    manifest = tmp_path / "assertions.yaml"
    manifest.write_text("this: [is: not: valid", encoding="utf-8")

    with pytest.raises(ReadinessContractLoadError, match="not valid YAML"):
        load_landing_zone_contract(manifest)


def test_contract_missing_a_design_area_fails_contract_validation(tmp_path: Path) -> None:
    manifest = tmp_path / "assertions.yaml"
    manifest.write_text(
        """
contractVersion: 1.0.0
assertions:
  - assertionId: tenant.reachable
    designArea: azure-billing-and-tenant
    description: The target subscription is reachable and can accept new resources.
    remediation: Confirm the subscription ID is correct and its billing state is Active.
    severity: blocking
""",
        encoding="utf-8",
    )

    with pytest.raises(ReadinessContractLoadError, match="failed contract validation"):
        load_landing_zone_contract(manifest)
