from __future__ import annotations

import logging
import os
from datetime import date

import httpx
import redis

from dealbot.search.client import SearchClient, SearchResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.search.brave.com/res/v1/web/search"
_REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

# Hard cap on Brave API calls per day. Set via env var; defaults to 200.
# Non-blocking: once hit, search returns empty results rather than erroring.
_DAILY_BUDGET = int(os.environ.get("BRAVE_DAILY_BUDGET", "200"))
_BUDGET_KEY_PREFIX = "brave_calls"


def _budget_key() -> str:
    return f"{_BUDGET_KEY_PREFIX}:{date.today().isoformat()}"


def _check_and_increment() -> bool:
    """
    Increment today's Brave call counter.
    Returns True if the call is allowed, False if the daily budget is exhausted.
    Expires the key at midnight automatically via TTL.
    """
    try:
        r = redis.from_url(_REDIS_URL, decode_responses=True)
        key = _budget_key()
        count = r.incr(key)
        if count == 1:
            r.expire(key, 86400)  # 24h TTL — auto-resets at next day
        return count <= _DAILY_BUDGET
    except Exception:
        # Redis unavailable — allow the call rather than blocking the pipeline
        logger.warning("BraveSearchClient: Redis unavailable, skipping budget check")
        return True


class BraveSearchClient(SearchClient):
    """Hits the Brave Web Search API and returns normalised SearchResult objects."""

    def __init__(self) -> None:
        self._api_key = os.environ["BRAVE_API_KEY"]

    async def search(self, query: str, n: int = 10) -> list[SearchResult]:
        if not _check_and_increment():
            logger.warning("BraveSearchClient: daily budget of %d calls exhausted, skipping query=%r", _DAILY_BUDGET, query)
            return []

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
