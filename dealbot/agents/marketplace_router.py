"""MarketplaceRouter

Given a query + curated marketplace list, an LLM picks the subset of sites
relevant to this query. Each site provides a `build_search_url(query) -> str`
function so the Explorer lands directly on that site's SERP (rather than
trying to drive JS-hostile home-page search UIs, which is fragile on sites
like Kijiji).

Per-site URL construction is the only per-site code in the pipeline. All
navigation from the SERP forward (pagination, category filters) is via role-
based accessibility selectors — zero per-site CSS. Extraction is entirely
LLM-driven from AX-tree snapshots, zero per-site DOM code.

Adding a new marketplace: append a `MarketplaceConfig` to
`CURATED_MARKETPLACES`, no other code changes required.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MarketplaceConfig:
    key: str                                        # canonical name
    display_name: str                               # for LLM prompt
    description: str                                # for LLM prompt
    build_search_url: Callable[[str], str]          # (query) -> SERP URL


@dataclass
class MarketplaceSearchTarget:
    """(marketplace, entry_url) — Explorer lands directly at `entry_url`,
    which is the marketplace's search results page for the query.
    """

    marketplace: str
    entry_url: str


class _RouteJSON(BaseModel):
    marketplaces: list[str]  # marketplace keys


# ---------------------------------------------------------------------------
# Curated marketplace registry
# ---------------------------------------------------------------------------

CURATED_MARKETPLACES: list[MarketplaceConfig] = [
    MarketplaceConfig(
        key="kijiji",
        display_name="Kijiji",
        description=(
            "Canadian classifieds; strong for used furniture, appliances, "
            "electronics, tools. Local pickup common. Excludes fashion/apparel."
        ),
        build_search_url=lambda q: (
            f"https://www.kijiji.ca/b-buy-sell/{q.replace(' ', '-').lower()}/k0c10"
        ),
    ),
    MarketplaceConfig(
        key="fb_marketplace",
        display_name="Facebook Marketplace",
        description=(
            "Local secondhand marketplace; strong for large items (furniture, "
            "vehicles, appliances), casual sales. Requires FB session."
        ),
        build_search_url=lambda q: (
            f"https://www.facebook.com/marketplace/search/?query={quote_plus(q)}"
        ),
    ),
    MarketplaceConfig(
        key="ebay",
        display_name="eBay",
        description=(
            "Global auction + fixed-price marketplace; strong for electronics, "
            "collectibles, hobbyist gear. Ships internationally."
        ),
        build_search_url=lambda q: (
            f"https://www.ebay.ca/sch/i.html?_nkw={quote_plus(q)}"
        ),
    ),
    MarketplaceConfig(
        key="craigslist",
        display_name="Craigslist",
        description=(
            "US-focused classifieds; strong for furniture, vehicles, tools, "
            "housing. Local pickup only. Best in major metro areas."
        ),
        build_search_url=lambda q: (
            f"https://toronto.craigslist.org/search/sss?query={quote_plus(q)}"
        ),
    ),
]


# ---------------------------------------------------------------------------
# Router prompt
# ---------------------------------------------------------------------------

def _render_router_prompt(query: str, marketplaces: list[MarketplaceConfig]) -> str:
    listing = "\n".join(
        f"- key: {m.key}\n  name: {m.display_name}\n  description: {m.description}"
        for m in marketplaces
    )
    return (
        f"Query: {query!r}\n\n"
        f"Available marketplaces:\n{listing}\n\n"
        'Return the subset of marketplace keys relevant to this query as JSON: '
        '{"marketplaces": ["key1", "key2"]}. '
        "Pick 2-3 marketplaces; skip those where the item category doesn't fit."
    )


MARKETPLACE_ROUTER_SYSTEM = """You route marketplace search queries to the
relevant secondhand marketplaces. Given a query and a list of available
marketplaces with descriptions, pick 2-3 marketplace keys where the item is
likely to be found.

Return JSON: {"marketplaces": ["key1", "key2"]}.

Rules:
  - Only return marketplace keys from the provided list.
  - Prefer 2-3 marketplaces; more spreads coverage too thin for the turn budget.
  - Match by item category (e.g. clothes -> apparel-focused sites, tools ->
    local classifieds).
  - Do not add commentary; return only the JSON."""


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

class MarketplaceRouter:
    def __init__(
        self,
        llm: LLMClient,
        marketplaces: list[MarketplaceConfig] | None = None,
    ) -> None:
        self.llm = llm
        self.marketplaces = marketplaces or CURATED_MARKETPLACES
        self._by_key = {m.key: m for m in self.marketplaces}

    async def route(
        self, query: str, spec: WatchlistContext,
    ) -> list[MarketplaceSearchTarget]:
        """Pick marketplaces for the query, build SERP URLs, return targets.

        Fallback on LLM failure: use all curated marketplaces (better wide
        coverage than dropping the query silently)."""
        messages = [
            {"role": "system", "content": MARKETPLACE_ROUTER_SYSTEM},
            {"role": "user", "content": _render_router_prompt(query, self.marketplaces)},
        ]
        try:
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning(
                "MarketplaceRouter: LLM failed for %r; using all curated: %s",
                query, exc,
            )
            return self._all_targets(query)

        try:
            data = json.loads(response.content)
            parsed = _RouteJSON.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "MarketplaceRouter: parse failed for %r; using all curated: %s",
                query, exc,
            )
            return self._all_targets(query)

        targets: list[MarketplaceSearchTarget] = []
        seen: set[str] = set()
        for key in parsed.marketplaces:
            if key in seen:
                continue
            config = self._by_key.get(key)
            if config is None:
                logger.info("MarketplaceRouter: LLM emitted unknown key %r", key)
                continue
            seen.add(key)
            targets.append(MarketplaceSearchTarget(
                marketplace=config.key,
                entry_url=config.build_search_url(query),
            ))

        if not targets:
            return self._all_targets(query)
        return targets

    def _all_targets(self, query: str) -> list[MarketplaceSearchTarget]:
        return [
            MarketplaceSearchTarget(
                marketplace=m.key, entry_url=m.build_search_url(query),
            )
            for m in self.marketplaces
        ]
