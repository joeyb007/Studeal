from __future__ import annotations

import logging
import os
from urllib.parse import urlparse

import httpx

from .base import SearchProvider, SearchResult

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"


class TavilyProvider(SearchProvider):
    """AI-native search via Tavily. Returns LLM-ready, ranked, scored results."""

    name = "tavily"
    cost_per_query_usd = 0.008

    def __init__(self, api_key: str | None = None) -> None:
        self._api_key = api_key or os.environ.get("TAVILY_API_KEY", "")

    def is_configured(self) -> bool:
        return bool(self._api_key)

    async def search(self, query: str, locale: str = "ca") -> list[SearchResult]:
        if not self.is_configured():
            logger.warning("TavilyProvider: TAVILY_API_KEY not set, skipping")
            return []

        suffixed = f"{query} canada" if locale == "ca" and "canada" not in query.lower() else query

        payload = {
            "api_key": self._api_key,
            "query": suffixed,
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
            "max_results": 10,
            "topic": "general",
        }

        try:
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.post(_TAVILY_URL, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except Exception as exc:
            logger.warning("TavilyProvider: request failed: %s", exc)
            return []

        results: list[SearchResult] = []
        for item in data.get("results", []):
            url = item.get("url", "")
            results.append(SearchResult(
                title=item.get("title", "").strip(),
                url=url,
                snippet=item.get("content", "").strip(),
                source_domain=urlparse(url).netloc.lower(),
                provider=self.name,
                raw=item,
            ))
        return results
