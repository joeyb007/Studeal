from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from .base import SearchProvider, SearchResult
from .firecrawl import FirecrawlProvider
from .google_resolver import GoogleShoppingResolver, ResolutionStats
from .serper import SerperProvider

logger = logging.getLogger(__name__)

# Cap per query — bounds resolution cost/latency per agent turn
_MAX_RESOLUTIONS_PER_QUERY = 8


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


def _needs_resolution(r: SearchResult) -> bool:
    """A Serper result needs Tier 1 resolution if it has a Google aggregator URL
    OR if it's missing real listed_price data (priceWas absent)."""
    if r.provider != "serper":
        return False
    is_google_aggregator = "google.com/search" in (r.url or "")
    missing_listed = (
        r.listed_price is None
        or r.sale_price is None
        or r.listed_price <= r.sale_price
    )
    return is_google_aggregator or missing_listed


class SearchRouter:
    """Parallel multi-provider search with dedup, cost tracking, and link resolution."""

    def __init__(self, providers: list[SearchProvider] | None = None) -> None:
        if providers is None:
            providers = [SerperProvider(), FirecrawlProvider()]
        self._providers = [p for p in providers if p.is_configured()]
        self._resolver = GoogleShoppingResolver()
        if not self._providers:
            logger.warning("SearchRouter: no providers configured — set SERPER_API_KEY / FIRECRAWL_API_KEY")

    @property
    def active_providers(self) -> list[str]:
        return [p.name for p in self._providers]

    async def search(self, query: str, locale: str = "ca") -> tuple[list[SearchResult], HuntCost]:
        """Fan out to all configured providers in parallel, then Tier 1 resolution
        on Serper results that need it, then merge + dedup."""
        if not self._providers:
            return [], HuntCost.empty()

        # Stage 1: parallel discovery across providers
        tasks = [p.search(query, locale=locale) for p in self._providers]
        results_per_provider = await asyncio.gather(*tasks, return_exceptions=True)

        cost = HuntCost(provider_calls={}, total_usd=0.0)
        all_results: list[SearchResult] = []

        for provider, results in zip(self._providers, results_per_provider):
            cost.provider_calls[provider.name] = 1
            cost.total_usd += provider.cost_per_query_usd
            if isinstance(results, BaseException):
                logger.warning("SearchRouter: %s raised %s", provider.name, results)
                continue
            all_results.extend(results)

        # Stage 2: Tier 1 link resolution for Serper aggregator URLs
        await self._resolve_serper_results(all_results, query)

        # Stage 3: dedup by URL
        merged: dict[str, SearchResult] = {}
        for r in all_results:
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

    async def _resolve_serper_results(self, results: list[SearchResult], query: str) -> None:
        """In-place enrichment: for Serper results needing resolution, fetch the
        Google page and update listed_price / real_discount_pct / url.
        Fails open — unresolved results retain their original Serper data."""
        candidates = [r for r in results if _needs_resolution(r)][:_MAX_RESOLUTIONS_PER_QUERY]
        if not candidates:
            return

        stats = ResolutionStats()
        resolution_tasks = [self._resolver.resolve(r.url) for r in candidates]
        resolutions = await asyncio.gather(*resolution_tasks, return_exceptions=True)

        for r, offer in zip(candidates, resolutions):
            if isinstance(offer, BaseException):
                logger.debug("google_resolver: exception for %s: %s", r.url[:60], offer)
                stats.attempted += 1
                stats.parse_failed += 1
                continue
            stats.record(offer)
            if not offer.success:
                continue
            if offer.listed_price is not None:
                r.listed_price = offer.listed_price
            if offer.direct_url:
                r.url = offer.direct_url

        logger.info(
            "google_resolver: query=%r resolved=%d/%d (%.0f%%) "
            "parse_fail=%d http_err=%d rate_limited=%d",
            query, stats.succeeded, stats.attempted, stats.success_rate * 100,
            stats.parse_failed, stats.http_errors, stats.rate_limited,
        )
