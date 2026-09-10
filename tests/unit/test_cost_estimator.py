"""T044 — cost composition, including FR-018's mandatory uncertainty band."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from groundwork_contracts.blueprint import FabricCapacitySku
from groundwork_shared.costing.estimator import (
    HOURS_PER_MONTH,
    compose_estimate,
    fabric_capacity_line,
)
from groundwork_shared.costing.retail_prices import RetailPricesClient

NOW = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)


def _fabric_item(meter_name: str, price: float) -> dict[str, object]:
    return {
        "meterName": meter_name,
        "productName": "Fabric Capacity",
        "skuName": meter_name,
        "serviceName": "Microsoft Fabric",
        "armRegionName": "australiaeast",
        "retailPrice": price,
        "unitOfMeasure": "1 Hour",
        "currencyCode": "AUD",
        "type": "Consumption",
    }


def _client_returning(items: list[dict[str, object]]) -> RetailPricesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"Items": items, "NextPageLink": None, "Count": len(items)})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RetailPricesClient(client=http_client)


# --- fabric_capacity_line -----------------------------------------------------------


async def test_fabric_capacity_line_computes_monthly_amount_from_cu_hours() -> None:
    client = _client_returning([_fabric_item("Data Warehouse Capacity Usage CU", 0.304326)])

    line = await fabric_capacity_line(client, FabricCapacitySku.F2, "australiaeast")

    expected = round(0.304326 * FabricCapacitySku.F2.capacity_units * HOURS_PER_MONTH, 2)
    assert line.monthly_amount == expected
    assert "F2" in (line.note or "")


async def test_fabric_capacity_line_ignores_overage_meter() -> None:
    client = _client_returning(
        [
            _fabric_item("Data Warehouse Capacity Usage CU", 0.304326),
            _fabric_item("Capacity Overage Capacity Usage CU", 0.912977),
        ]
    )

    line = await fabric_capacity_line(client, FabricCapacitySku.F2, "australiaeast")

    expected = round(0.304326 * FabricCapacitySku.F2.capacity_units * HOURS_PER_MONTH, 2)
    assert line.monthly_amount == expected


async def test_fabric_capacity_line_ignores_on_demand_meters() -> None:
    """[VERIFIED] 2026-08-21: Microsoft's catalog restructure added 86 '...On-Demand Usage CU'
    meters at rates distinct from the Capacity Usage family (e.g. Copilot On-Demand at
    0.259947 AUD vs the capacity rate 0.303271 AUD in australiaeast). Only the Capacity Usage
    family is the per-CU-hour basis for a provisioned capacity; the old single-rate assertion
    must not fire just because On-Demand meters are present."""
    client = _client_returning(
        [
            _fabric_item("Data Warehouse Capacity Usage CU", 0.303271),
            _fabric_item("Copilot and AI On-Demand Usage CU", 0.259947),
            _fabric_item("Eventhouse On-Demand Usage CU", 0.353527),
            _fabric_item("Capacity Overage Capacity Usage CU", 0.909813),
        ]
    )

    line = await fabric_capacity_line(client, FabricCapacitySku.F2, "australiaeast")

    expected = round(0.303271 * FabricCapacitySku.F2.capacity_units * HOURS_PER_MONTH, 2)
    assert line.monthly_amount == expected


async def test_fabric_capacity_line_raises_when_no_pricing_found() -> None:
    client = _client_returning([])

    with pytest.raises(ValueError, match="no Fabric Capacity consumption pricing"):
        await fabric_capacity_line(client, FabricCapacitySku.F2, "australiaeast")


async def test_fabric_capacity_line_raises_when_meters_disagree() -> None:
    """Guards the assumption the module docstring names explicitly: if this ever fires for real,
    the flat per-CU-hour-rate assumption has broken and the estimate would otherwise be silently
    wrong rather than loudly wrong."""
    client = _client_returning(
        [
            _fabric_item("Data Warehouse Capacity Usage CU", 0.304326),
            _fabric_item("Eventhouse Capacity Usage CU", 0.31),
        ]
    )

    with pytest.raises(ValueError, match="disagree on price"):
        await fabric_capacity_line(client, FabricCapacitySku.F2, "australiaeast")


async def test_fabric_capacity_line_scales_with_capacity_units() -> None:
    client_f2 = _client_returning([_fabric_item("m", 0.304326)])
    client_f4 = _client_returning([_fabric_item("m", 0.304326)])

    line_f2 = await fabric_capacity_line(client_f2, FabricCapacitySku.F2, "australiaeast")
    line_f4 = await fabric_capacity_line(client_f4, FabricCapacitySku.F4, "australiaeast")

    # Independently rounded to 2dp per SKU, so F4 is approximately — not exactly — double F2's
    # amount (rounding each line separately does not distribute linearly).
    assert line_f4.monthly_amount == pytest.approx(line_f2.monthly_amount * 2, abs=0.01)


# --- compose_estimate ----------------------------------------------------------------


def test_compose_estimate_totals_match_components() -> None:
    from groundwork_contracts.approval import CostComponent, CostComponentKind

    fabric_line = CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=444.31)

    estimate = compose_estimate(fabric_line, now=NOW)

    assert estimate.monthly_total == pytest.approx(
        sum(c.monthly_amount for c in estimate.components)
    )


def test_compose_estimate_carries_a_meaningful_uncertainty_band() -> None:
    from groundwork_contracts.approval import CostComponent, CostComponentKind

    fabric_line = CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=444.31)

    estimate = compose_estimate(fabric_line, now=NOW)

    assert estimate.uncertainty_lower_pct > 0
    assert estimate.uncertainty_upper_pct > 0


def test_compose_estimate_fabric_line_is_present_and_dominant() -> None:
    """Research notes § V-001: capacity is the largest cost and must be presented, not buried."""
    from groundwork_contracts.approval import CostComponent, CostComponentKind

    fabric_line = CostComponent(component=CostComponentKind.FABRIC_CAPACITY, monthly_amount=444.31)

    estimate = compose_estimate(fabric_line, now=NOW)

    fabric_component = next(
        c for c in estimate.components if c.component is CostComponentKind.FABRIC_CAPACITY
    )
    assert fabric_component.monthly_amount == max(c.monthly_amount for c in estimate.components)
