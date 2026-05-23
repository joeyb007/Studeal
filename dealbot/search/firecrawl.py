from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from .base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

_FIRECRAWL_URL = "https://api.firecrawl.dev/v2/search"

DEAL_EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "deals": {
            "type": "array",
            "description": "All distinct product deals, sales, or discounted offers on this page. One page may contain multiple deals (e.g., deal-aggregator threads, listicle articles).",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "Product name or deal title",
                    },
                    "sale_price": {
                        "type": "number",
                        "description": "Current discounted price (number only, no currency symbol)",
                    },
                    "listed_price": {
                        "type": "number",
                        "description": "Original/MSRP price if shown, else same as sale_price",
                    },
                    "currency": {
                        "type": "string",
                        "description": "Currency code (USD, CAD, etc.)",
                    },
                    "condition": {
                        "type": "string",
                        "enum": ["new", "refurb", "used", "unknown"],
                    },
                    "retailer": {
                        "type": "string",
                        "description": "Retailer/source name (Amazon, Best Buy, etc.)",
                    },
                    "product_url": {
                        "type": "string",
                        "description": "Direct link to the product/deal page if present, else empty",
                    },
                },
                "required": ["title", "sale_price"],
            },
        }
    },
    "required": ["deals"],
}


class FirecrawlProvider(SearchProvider):
    """Firecrawl /v2/search with schema-based structured extraction.

    Firecrawl searches the web, scrapes top results, and runs LLM extraction
    against DEAL_EXTRACTION_SCHEMA in one call. Each scraped URL can yield
    0–N deals (aggregator pages with multiple offers handled natively via the
    array-typed schema).
    """

    name = "firecrawl"
    cost_per_query_usd = 0.015

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("FIRECRAWL_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, locale: str = "ca") -> list[SearchResult]:
        if not self.is_configured():
            logger.warning("FirecrawlProvider: FIRECRAWL_API_KEY not set, skipping")
            return []

        suffixed = f"{query} canada" if locale == "ca" and "canada" not in query.lower() else query

        payload = {
            "query": suffixed,
            "limit": 10,
            "scrapeOptions": {
                "formats": [
                    {
                        "type": "json",
                        "schema": DEAL_EXTRACTION_SCHEMA,
                    }
                ],
                "onlyMainContent": True,
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                resp = await client.post(_FIRECRAWL_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("FirecrawlProvider: request failed: %s", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("data", []):
            page_url = item.get("url", "")
            page_title = item.get("title", "")
            json_blob = item.get("json") or {}
            deals = json_blob.get("deals") or []

            if not deals:
                continue  # page returned no extractable deals — skip

            for d in deals:
                title = (d.get("title") or "").strip()
                sale_price = d.get("sale_price")
                if not title or sale_price is None:
                    continue

                listed_price = d.get("listed_price")
                if listed_price is None or listed_price < sale_price:
                    listed_price = sale_price

                # Use the product_url if extracted, else the page URL
                deal_url = (d.get("product_url") or "").strip() or page_url
                retailer = (d.get("retailer") or "").strip() or urlparse(deal_url).netloc.lower()

                results.append(SearchResult(
                    title=title,
                    url=deal_url,
                    snippet=page_title,
                    sale_price=float(sale_price),
                    listed_price=float(listed_price),
                    source_domain=retailer,
                    provider=self.name,
                    raw={"page_url": page_url, "deal": d},
                ))

        logger.info(
            "FirecrawlProvider: query=%r pages=%d deals_extracted=%d",
            suffixed, len(data.get("data", [])), len(results),
        )
        return results
