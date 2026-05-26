from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .base import SearchProvider, SearchResult
from .firecrawl import FirecrawlProvider
from .serper import SerperProvider

logger = logging.getLogger(__name__)


@dataclass
class HuntCost:
    """Per-hunt cost breakdown for telemetry and pro-tier pricing analysis."""

    provider_calls: dict[str, int]
    total_usd: float

    @classmethod
    def empty(cls) -> "HuntCost":
        return cls(provider_calls={}, total_usd=0.0)


def _normalize_url(url: str) -> str:
    """Canonicalize URL for dedup — strip query strings + trailing slash."""
    if not url:
        return ""
    base = url.split("?")[0].split("#")[0].rstrip("/")
    return base.lower()


class SearchRouter:
    """Parallel multi-provider search with dedup and cost tracking.

    Link resolution (Google aggregator URL → direct retailer URL) is handled
    downstream in score_and_persist_node, per-deal in the fan-out — not here.
    Keeps the router focused on aggregation.
    """

    def __init__(self, providers: list[SearchProvider] | None = None) -> None:
        if providers is None:
            providers = [SerperProvider(), FirecrawlProvider()]
        self._providers = [p for p in providers if p.is_configured()]
        if not self._providers:
            logger.warning("SearchRouter: no providers configured — set SERPER_API_KEY / FIRECRAWL_API_KEY")

    @property
    def active_providers(self) -> list[str]:
        return [p.name for p in self._providers]

    async def search(self, query: str, locale: str = "ca") -> tuple[list[SearchResult], HuntCost]:
        """Fan out to all configured providers in parallel, merge + dedup by URL."""
        if not self._providers:
            return [], HuntCost.empty()

        tasks = [p.search(query, locale=locale) for p in self._providers]
        results_per_provider = await asyncio.gather(*tasks, return_exceptions=True)

        cost = HuntCost(provider_calls={}, total_usd=0.0)
        merged: dict[str, SearchResult] = {}

        for provider, results in zip(self._providers, results_per_provider):
            cost.provider_calls[provider.name] = 1
            cost.total_usd += provider.cost_per_query_usd
            if isinstance(results, BaseException):
                logger.warning("SearchRouter: %s raised %s", provider.name, results)
                continue
            for r in results:
                key = _normalize_url(r.url)
                if not key:
                    continue
                if key in merged:
                    existing = merged[key]
                    if r.sale_price is not None and existing.sale_price is None:
                        merged[key] = r
                else:
                    merged[key] = r

        final = list(merged.values())
        logger.info(
            "SearchRouter: query=%r providers=%s merged=%d cost=$%.4f",
            query, [p.name for p in self._providers], len(final), cost.total_usd,
        )
        return final, cost
