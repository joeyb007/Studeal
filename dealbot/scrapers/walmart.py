"""Walmart Affiliate API scraper.

Requires approval from Walmart's affiliate program (Impact/CJ).
Returns empty list gracefully if WALMART_CLIENT_ID is not set.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from dealbot.schemas import Condition, DealRaw

logger = logging.getLogger(__name__)

_SEARCH_URL = "https://developer.api.walmart.com/api-proxy/service/affil/product/v2/search"


async def search_walmart(keyword: str, limit: int = 10) -> list[DealRaw]:
    client_id = os.environ.get("WALMART_CLIENT_ID")
    if not client_id:
        return []

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _SEARCH_URL,
                headers={
                    "WM_CONSUMER.ID": client_id,
                    "WM_SEC.KEY_VERSION": "1",
                },
                params={
                    "query": keyword,
                    "numItems": min(limit, 25),
                    "sort": "price",
                    "order": "asc",
                    "format": "json",
                },
            )
            resp.raise_for_status()
        return _parse_items(resp.json())
    except Exception as exc:
        logger.warning("search_walmart: failed for %r: %s", keyword, exc)
        return []


def _parse_items(data: dict[str, Any]) -> list[DealRaw]:
    deals = []
    for item in data.get("items", []):
        try:
            sale_price = float(item.get("salePrice") or item.get("msrp") or 0)
            if sale_price <= 0:
                continue

            listed_price = float(item.get("msrp") or sale_price)
            if listed_price < sale_price:
                listed_price = sale_price

            url = item.get("productUrl") or item.get("addToCartUrl") or ""

            deals.append(DealRaw(
                source="Walmart",
                title=item["name"],
                url=url or None,
                listed_price=listed_price,
                sale_price=sale_price,
                condition=Condition.new,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return deals
