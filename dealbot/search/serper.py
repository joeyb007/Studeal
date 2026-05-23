from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlparse

import httpx

from .base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

_SERPER_URL = "https://google.serper.dev/shopping"

_PRICE_RE = re.compile(r"[\d,]+(?:\.\d{1,2})?")


def _parse_price(s: str | None) -> float | None:
    if not s:
        return None
    match = _PRICE_RE.search(s.replace(",", ""))
    if not match:
        return None
    try:
        return float(match.group())
    except ValueError:
        return None


class SerperProvider(SearchProvider):
    """Google Shopping via Serper. Returns structured price + source + image."""

    name = "serper"
    cost_per_query_usd = 0.001

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("SERPER_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, locale: str = "ca") -> list[SearchResult]:
        if not self.is_configured():
            logger.warning("SerperProvider: SERPER_API_KEY not set, skipping")
            return []

        payload = {
            "q": query,
            "gl": locale,
            "hl": "en",
            "num": 100,
        }
        headers = {
            "X-API-KEY": self._api_key,
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(_SERPER_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("SerperProvider: request failed: %s", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("shopping", []):
            url = item.get("link", "")
            sale = _parse_price(item.get("price"))
            listed = _parse_price(item.get("priceWas")) or sale
            results.append(SearchResult(
                title=item.get("title", "").strip(),
                url=url,
                snippet=item.get("snippet", "") or item.get("delivery", ""),
                sale_price=sale,
                listed_price=listed,
                source_domain=item.get("source", "") or urlparse(url).netloc.lower(),
                image_url=item.get("imageUrl"),
                provider=self.name,
                raw=item,
            ))
        return results
