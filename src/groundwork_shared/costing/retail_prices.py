"""Azure Retail Prices API client (T043).

Verified 2026-07-31 against learn.microsoft.com/rest/api/cost-management/retail-prices/
azure-retail-prices (updated 2026-01-06): the endpoint is public and unauthenticated — no Entra
token, no credential, nothing to manage. Confirmed live against the real endpoint, not just the
docs. Re-verified 2026-08-21 after Microsoft's catalog restructure split the former single-rate
meter family (see ``estimator.py``'s [VERIFIED] block for the current shape and how
``fabric_capacity_line`` filters it).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

RETAIL_PRICES_ENDPOINT = "https://prices.azure.com/api/retail/prices"
API_VERSION = "2023-01-01-preview"


@dataclass(frozen=True, slots=True)
class RetailPriceItem:
    meter_name: str
    product_name: str
    sku_name: str
    service_name: str
    arm_region_name: str
    retail_price: float
    unit_of_measure: str
    currency_code: str
    price_type: str


def _parse_item(raw: dict[str, Any]) -> RetailPriceItem:
    return RetailPriceItem(
        meter_name=raw["meterName"],
        product_name=raw["productName"],
        sku_name=raw["skuName"],
        service_name=raw["serviceName"],
        arm_region_name=raw["armRegionName"],
        retail_price=float(raw["retailPrice"]),
        unit_of_measure=raw["unitOfMeasure"],
        currency_code=raw["currencyCode"],
        price_type=raw["type"],
    )


class RetailPricesClient:
    """Thin wrapper over the public Azure Retail Prices API, with pagination followed to
    completion — a caller must never have to remember ``NextPageLink`` exists."""

    def __init__(
        self, *, currency_code: str = "AUD", client: httpx.AsyncClient | None = None
    ) -> None:
        self._currency_code = currency_code
        self._client = client or httpx.AsyncClient(timeout=15.0)
        self._owns_client = client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def query(self, odata_filter: str) -> list[RetailPriceItem]:
        """Fetch every page of retail prices matching ``odata_filter``.

        ``odata_filter`` is an OData ``$filter`` expression, e.g. ``armRegionName eq
        'australiaeast' and serviceName eq 'Microsoft Fabric'``.
        """
        items: list[RetailPriceItem] = []
        url: str | None = RETAIL_PRICES_ENDPOINT
        params: dict[str, str] | None = {
            "api-version": API_VERSION,
            "currencyCode": self._currency_code,
            "$filter": odata_filter,
        }

        while url:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
            payload = response.json()
            items.extend(_parse_item(raw) for raw in payload.get("Items", []))
            # NextPageLink is a complete URL with its own query string; params must not be
            # re-applied on top of it or the filter would be duplicated.
            url = payload.get("NextPageLink")
            params = None

        return items
