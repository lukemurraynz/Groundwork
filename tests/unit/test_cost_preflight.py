"""T075a — execution-time cost re-check with re-approval and escalation (FR-019)."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from groundwork_contracts.plan import DeploymentPlan
from groundwork_orchestrator.engine.cost_preflight import (
    CostPreflight,
    CostPreflightError,
    recheck_cost,
)
from groundwork_shared.config.settings import GovernanceSettings
from groundwork_shared.costing.retail_prices import RetailPricesClient

NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _fabric_item(price: float) -> dict[str, object]:
    return {
        "meterName": "Data Warehouse Capacity Usage CU",
        "productName": "Fabric Capacity",
        "skuName": "F2",
        "serviceName": "Microsoft Fabric",
        "armRegionName": "australiaeast",
        "retailPrice": price,
        "unitOfMeasure": "1 Hour",
        "currencyCode": "AUD",
        "type": "Consumption",
    }


def _client_priced_at(hourly_rate: float) -> RetailPricesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"Items": [_fabric_item(hourly_rate)], "NextPageLink": None, "Count": 1},
        )

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RetailPricesClient(client=http_client)


def _failing_client() -> RetailPricesClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "service unavailable"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RetailPricesClient(client=http_client)


def _governance(*, threshold: float = 100_000.0) -> GovernanceSettings:
    return GovernanceSettings(
        approval_threshold_aud=threshold,
        approver_role="Groundwork.Approver",
        default_tenant_concurrency_cap=3,
    )


# --- recheck_cost --------------------------------------------------------------------


async def test_recheck_within_band_reports_no_escalation(valid_plan: DeploymentPlan) -> None:
    # valid_plan's approved total is 412.50 AUD, band [10%, 20%] -> [371.25, 495.0].
    # 0.221 AUD/CU-hour * 2 CU * 730h = 322.66 -> + 90 baseline = 412.66, inside the band.
    client = _client_priced_at(0.221)

    result = await recheck_cost(
        plan=valid_plan, retail_prices_client=client, governance=_governance(), now=NOW
    )

    assert result.within_band is True
    assert result.crosses_second_approver_threshold is False


async def test_recheck_outside_band_below_threshold(valid_plan: DeploymentPlan) -> None:
    # 0.5 AUD/CU-hour * 2 * 730 = 730 -> + 90 = 820, well outside [371.25, 495.0], but the
    # governance threshold here (100,000) is nowhere near crossed.
    client = _client_priced_at(0.5)

    result = await recheck_cost(
        plan=valid_plan, retail_prices_client=client, governance=_governance(), now=NOW
    )

    assert result.within_band is False
    assert result.crosses_second_approver_threshold is False
    assert "outside the approved estimate's own uncertainty band" in result.detail


async def test_recheck_outside_band_and_crosses_threshold(valid_plan: DeploymentPlan) -> None:
    client = _client_priced_at(0.5)  # recomputed total 820 AUD

    result = await recheck_cost(
        plan=valid_plan,
        retail_prices_client=client,
        governance=_governance(threshold=800.0),
        now=NOW,
    )

    assert result.within_band is False
    assert result.crosses_second_approver_threshold is True


async def test_recheck_raises_on_client_failure(valid_plan: DeploymentPlan) -> None:
    client = _failing_client()

    with pytest.raises(CostPreflightError, match="cost preflight could not be completed"):
        await recheck_cost(
            plan=valid_plan, retail_prices_client=client, governance=_governance(), now=NOW
        )


# --- CostPreflight (the injectable wrapper Sequencer actually calls) -----------------


async def test_cost_preflight_wrapper_delegates_to_recheck_cost(valid_plan: DeploymentPlan) -> None:
    client = _client_priced_at(0.221)
    preflight = CostPreflight(retail_prices_client=client, governance=_governance())

    result = await preflight(valid_plan, now=NOW)

    assert result.within_band is True
