"""Cost composition (T044): AUD line items with a mandatory uncertainty band (FR-018).

The Fabric capacity line is the platform's dominant cost and is live-priced against the Azure
Retail Prices API — the research notes § V-001 flag it as "the largest and must be presented, not
buried," so an assumed or stale figure here would be the single worst place to have one. Storage,
networking, and monitoring are documented baseline estimates, not live-priced today; see
``BASELINE_STORAGE_AUD`` and its siblings below for why that is a disclosed scope boundary rather
than a silent simplification.
"""

from __future__ import annotations

from datetime import datetime

from groundwork_contracts.approval import CostComponent, CostComponentKind, CostEstimate
from groundwork_contracts.blueprint import FabricCapacitySku
from groundwork_shared.costing.retail_prices import RetailPricesClient

# Standard Azure billing convention for converting an hourly rate to a monthly estimate
# (365.25 * 24 / 12 ≈ 730.5, floored to the commonly used round figure).
HOURS_PER_MONTH = 730

# Baseline, NOT live-priced estimates for the platform's smaller components. Unlike Fabric
# capacity, these are order-of-magnitude starting figures for a minimum-sized landing zone (Key
# Vault, Log Analytics, Storage, VNet, Private Endpoints at F2/dev scale) rather than a Retail
# Prices API query — a real, disclosed follow-up (each would need its own verified meter query,
# the same work fabric_capacity_line already did), not a fact this module claims to have.
# FR-018's mandatory uncertainty band exists precisely to cover estimates like these honestly.
BASELINE_STORAGE_AUD = 25.0
BASELINE_NETWORKING_AUD = 20.0
BASELINE_MONITORING_AUD = 45.0

UNCERTAINTY_LOWER_PCT = 10.0
UNCERTAINTY_UPPER_PCT = 25.0


async def fabric_capacity_line(
    client: RetailPricesClient, sku: FabricCapacitySku, region: str
) -> CostComponent:
    """The Fabric capacity cost line, live-priced against the Azure Retail Prices API.

    [VERIFIED] 2026-08-21, live against ``prices.azure.com`` (api-version 2023-01-01-preview,
    australiaeast): the query below returns 179 meters — 92 "...Capacity Usage CU" meters sharing
    exactly one rate (0.303271 AUD/CU-hour on the date verified), 86 "...On-Demand Usage CU"
    meters at eight further distinct rates, and the Capacity Overage meter at 3x. Microsoft added
    the On-Demand family after 2026-07-31 (when all 93 non-overage meters still shared one rate,
    per this function's original assertion), which is what broke that assumption.

    The Capacity Usage rate is the correct basis, verified three ways the same day:

    1. No per-SKU PAYG capacity meter exists anywhere in the catalog anymore — a global
       ``skuName eq 'F2'`` query returns only Virtual Machines and HDInsight meters; the
       ``Fabric Capacity CU`` meter (armSkuName ``Fabric_Capacity_CU_Hour``) is published as
       Reservation type only.
    2. Microsoft's own pricing page (azure.microsoft.com/pricing/details/microsoft-fabric)
       prices PAYG capacity linearly per CU-hour — F2 = US$262.80/month = $0.18/CU-h x 2 CUs x
       730h — exactly this function's formula.
    3. The reservation cross-check: A$1,579.90/CU/year (1-year term) implies a discounted rate
       consistent with the documented ~41% saving against PAYG.

    Filtering by meter-name suffix (excluding both overage and on-demand) keeps the single-rate
    agreement a live self-check rather than a silent assumption: if the Capacity Usage family ever
    stops agreeing, this raises instead of producing a quietly wrong FR-018/FR-019 figure.
    """
    items = await client.query(
        f"armRegionName eq '{region}' and serviceName eq 'Microsoft Fabric' and "
        f"productName eq 'Fabric Capacity' and type eq 'Consumption'"
    )
    capacity_usage = [
        item
        for item in items
        if "overage" not in item.meter_name.lower() and "on-demand" not in item.meter_name.lower()
    ]
    if not capacity_usage:
        raise ValueError(
            f"no Fabric Capacity consumption pricing found for region {region!r}; the Retail "
            f"Prices API returned no usable meters"
        )

    distinct_rates = {item.retail_price for item in capacity_usage}
    if len(distinct_rates) > 1:
        raise ValueError(
            f"Fabric Capacity consumption meters in {region!r} disagree on price "
            f"({sorted(distinct_rates)}); this estimator assumes one per-capacity-unit-hour rate "
            f"and that assumption no longer holds"
        )

    hourly_rate_per_cu = next(iter(distinct_rates))
    monthly_amount = round(hourly_rate_per_cu * sku.capacity_units * HOURS_PER_MONTH, 2)

    return CostComponent(
        component=CostComponentKind.FABRIC_CAPACITY,
        monthly_amount=monthly_amount,
        note=(
            f"{sku.value} = {sku.capacity_units} capacity units at {hourly_rate_per_cu} "
            f"{capacity_usage[0].currency_code}/CU-hour, {HOURS_PER_MONTH}h/month"
        ),
    )


def compose_estimate(fabric_line: CostComponent, *, now: datetime) -> CostEstimate:
    """Assemble the full FR-018 cost estimate from the Fabric line plus baseline components."""
    components = (
        fabric_line,
        CostComponent(component=CostComponentKind.STORAGE, monthly_amount=BASELINE_STORAGE_AUD),
        CostComponent(
            component=CostComponentKind.NETWORKING, monthly_amount=BASELINE_NETWORKING_AUD
        ),
        CostComponent(
            component=CostComponentKind.MONITORING, monthly_amount=BASELINE_MONITORING_AUD
        ),
    )
    monthly_total = round(sum(component.monthly_amount for component in components), 2)

    return CostEstimate(
        components=components,
        monthly_total=monthly_total,
        uncertainty_lower_pct=UNCERTAINTY_LOWER_PCT,
        uncertainty_upper_pct=UNCERTAINTY_UPPER_PCT,
        basis=(
            f"Fabric capacity from Azure Retail Prices API snapshot {now.date().isoformat()}; "
            f"storage, networking, and monitoring are baseline minimum-sizing estimates, not "
            f"live-priced"
        ),
        computed_at=now,
        # FR-013d's disclosure is a separate, later step (T081b) — this estimate does not itself
        # claim the disclosure happened.
        powerbi_viewer_licensing_disclosed=False,
    )
