from __future__ import annotations

import logging
import os

import httpx

from dealbot.search.client import SearchClient, SearchResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.search.brave.com/res/v1/web/search"


class BraveSearchClient(SearchClient):
    """Hits the Brave Web Search API and returns normalised SearchResult objects."""

    def __init__(self) -> None:
        self._api_key = os.environ["BRAVE_API_KEY"]

    async def search(self, query: str, n: int = 10) -> list[SearchResult]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "X-Subscription-Token": self._api_key,
        }
        params = {"q": query, "count": min(n, 20)}

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(_BASE_URL, headers=headers, params=params)
                resp.raise_for_status()

            data = resp.json()
            raw_results = data.get("web", {}).get("results", [])

            return [
                SearchResult(
                    title=r.get("title", ""),
                    url=r.get("url", ""),
                    description=r.get("description", ""),
                    age=r.get("age"),
                )
                for r in raw_results
                if r.get("url") and r.get("title")
            ]
        except Exception:
            logger.warning("BraveSearchClient: search failed for query=%r", query)
            return []
