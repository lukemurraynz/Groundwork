"""T043 — the Retail Prices API client's pagination and parsing.

``httpx.MockTransport`` stands in for the network — this is httpx's own supported testing
mechanism for its transport layer, not a mock of this module's logic, matching how
``fastapi.testclient.TestClient`` (already used in ``tests/unit/test_correlation.py``) substitutes
an ASGI transport rather than a real socket.
"""

from __future__ import annotations

import httpx
import pytest

from groundwork_shared.costing.retail_prices import RetailPricesClient

ITEM_TEMPLATE = {
    "meterName": "Data Warehouse Capacity Usage CU",
    "productName": "Fabric Capacity",
    "skuName": "Data Warehouse Capacity Usage CU",
    "serviceName": "Microsoft Fabric",
    "armRegionName": "australiaeast",
    "retailPrice": 0.304326,
    "unitOfMeasure": "1 Hour",
    "currencyCode": "AUD",
    "type": "Consumption",
}


def _page(items: list[dict[str, object]], next_link: str | None = None) -> dict[str, object]:
    return {"Items": items, "NextPageLink": next_link, "Count": len(items)}


async def test_query_parses_a_single_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([ITEM_TEMPLATE]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RetailPricesClient(client=http_client)
        items = await client.query("serviceName eq 'Microsoft Fabric'")

    assert len(items) == 1
    assert items[0].retail_price == 0.304326
    assert items[0].currency_code == "AUD"
    assert items[0].service_name == "Microsoft Fabric"


async def test_query_follows_pagination_to_completion() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                200, json=_page([ITEM_TEMPLATE], next_link="https://prices.azure.com/next")
            )
        return httpx.Response(200, json=_page([ITEM_TEMPLATE, ITEM_TEMPLATE]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RetailPricesClient(client=http_client)
        items = await client.query("serviceName eq 'Microsoft Fabric'")

    assert calls == 2
    assert len(items) == 3


async def test_query_raises_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="service unavailable")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RetailPricesClient(client=http_client)
        with pytest.raises(httpx.HTTPStatusError):
            await client.query("serviceName eq 'Microsoft Fabric'")


async def test_query_with_no_matching_items_returns_empty_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_page([]))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = RetailPricesClient(client=http_client)
        items = await client.query("serviceName eq 'Nonexistent Service'")

    assert items == []
