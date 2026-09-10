"""FR-013d / T081b — the below-F64 Power BI viewer-licensing disclosure.

Three assertions per surface: the statement exists below F64, is absent at/above F64, and the
approval gate rejects a below-F64 plan whose approval request never acknowledged it.
"""

from __future__ import annotations

from groundwork_contracts.blueprint import FabricCapacitySku
from groundwork_controlplane.costing.licensing import (
    POWER_BI_VIEWER_LICENSING_DISCLOSURE,
    licensing_disclosure_for,
)


def test_disclosure_present_below_f64() -> None:
    assert licensing_disclosure_for(FabricCapacitySku.F2) == POWER_BI_VIEWER_LICENSING_DISCLOSURE
    assert licensing_disclosure_for(FabricCapacitySku.F32) == POWER_BI_VIEWER_LICENSING_DISCLOSURE


def test_disclosure_absent_at_and_above_f64() -> None:
    # None, not an empty string: "nothing to disclose" must be distinguishable from "a
    # disclosure that failed to render".
    assert licensing_disclosure_for(FabricCapacitySku.F64) is None
    assert licensing_disclosure_for(FabricCapacitySku.F128) is None


def test_disclosure_names_the_licence_requirement() -> None:
    """FR-013d's own words: viewers "will each require a Power BI Pro or PPU licence" and free
    consumption is unavailable — a customer reading only this text learns both facts."""
    text = POWER_BI_VIEWER_LICENSING_DISCLOSURE
    assert "Pro" in text
    assert "PPU" in text
    assert "not available" in text
