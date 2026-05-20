from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from .base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

_FIRECRAWL_URL = "https://api.firecrawl.dev/v1/search"


class FirecrawlProvider(SearchProvider):
    """Firecrawl search — returns clean markdown content alongside results.

    Use for queries where Tavily/Serper return thin snippets and we want
    extracted page content for downstream LLM scoring.
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
            "scrapeOptions": {"formats": ["markdown"]},
        }
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(_FIRECRAWL_URL, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("FirecrawlProvider: request failed: %s", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("data", []):
            url = item.get("url", "")
            snippet = item.get("markdown", "") or item.get("description", "")
            results.append(SearchResult(
                title=item.get("title", "").strip(),
                url=url,
                snippet=snippet[:1500],
                source_domain=urlparse(url).netloc.lower(),
                provider=self.name,
                raw=item,
            ))
        return results
