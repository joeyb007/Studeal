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
import os
from dataclasses import dataclass
from typing import Callable
from urllib.parse import quote_plus

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

# FB Marketplace is city-scoped in its URL path; default to the product's
# home market. (Per-user locality is a Phase-4 concern — see spec §3b.)
_FB_CITY = os.environ.get("FB_MARKETPLACE_CITY", "toronto")


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class MarketplaceConfig:
    key: str                                        # canonical name
    display_name: str                               # for LLM prompt
    description: str                                # for LLM prompt
    build_search_url: Callable[[str], str]          # (query) -> SERP URL
    # Some sites serve their public page to search-engine referrals and an
    # auth wall to cold direct hits (measured on FB Marketplace 2026-07-30:
    # 57 elems walled vs 562 elems with a Google referer). Config data, not
    # per-site logic — the explorer just sets the header.
    entry_referer: str | None = None
    # Thumbnail capture (probe-verified 2026-08-02, scripts/probe_listing_images.py).
    # Pattern identifies listing-card anchors on gallery pages; hosts whitelist
    # the CDN a product image may live on (suffix match). None pattern = site
    # not yet probed, capture skipped entirely.
    listing_href_pattern: str | None = None
    image_cdn_hosts: tuple[str, ...] = ()


@dataclass
class MarketplaceSearchTarget:
    """(marketplace, entry_url) — Explorer lands directly at `entry_url`,
    which is the marketplace's search results page for the query.
    """

    marketplace: str
    entry_url: str
    entry_referer: str | None = None


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
        listing_href_pattern="/v-",
        image_cdn_hosts=("media.kijiji.ca",),
    ),
    MarketplaceConfig(
        key="fb_marketplace",
        display_name="Facebook Marketplace",
        description=(
            "Local secondhand marketplace; strong for large items (furniture, "
            "vehicles, appliances), casual sales. Requires FB session."
        ),
        # City-scoped: the un-scoped /marketplace/search/ URL geolocates by
        # exit IP and returned Bay Area listings on a Canadian hunt.
        build_search_url=lambda q: (
            f"https://www.facebook.com/marketplace/{_FB_CITY}/search/?query={quote_plus(q)}"
        ),
        entry_referer="https://www.google.com/",
        listing_href_pattern="/marketplace/item/",
        image_cdn_hosts=(".fbcdn.net",),
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
        listing_href_pattern="/itm/",
        image_cdn_hosts=("i.ebayimg.com",),
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
        listing_href_pattern="/d/",
        image_cdn_hosts=("images.craigslist.org",),
    ),
    # --- Retailer refurb / open-box (ship Canada-wide; no geography). Added
    # --- in the 2026-07-30 site expansion; every entry below survived a
    # --- zero-tuning first-exposure eval (docs/evals/results.md expand_* rows).
    MarketplaceConfig(
        key="bestbuy_outlet",
        display_name="Best Buy Canada (open box / refurbished)",
        description=(
            "Big-box electronics retailer's discounted open-box and certified "
            "refurbished stock: laptops, TVs, audio, appliances. Ships Canada-wide."
        ),
        build_search_url=lambda q: (
            f"https://www.bestbuy.ca/en-ca/search?search={quote_plus(q + ' open box')}"
        ),
    ),
    MarketplaceConfig(
        key="canada_computers",
        display_name="Canada Computers (open box)",
        description=(
            "PC-focused retailer: open-box laptops, components, monitors, "
            "peripherals. Strong for PC/gaming hardware, not Apple."
        ),
        build_search_url=lambda q: (
            f"https://www.canadacomputers.com/en/search?s={quote_plus(q + ' open box')}"
        ),
    ),
    MarketplaceConfig(
        key="visions_openbox",
        display_name="Visions Electronics (open box)",
        description=(
            "Canadian electronics chain's open-box deals: TVs, home audio, "
            "headphones, car tech."
        ),
        build_search_url=lambda q: (
            f"https://www.visions.ca/catalogsearch/result/?q={quote_plus(q + ' open box')}"
        ),
    ),
    MarketplaceConfig(
        key="newegg_ca",
        display_name="Newegg Canada (refurbished / open box)",
        description=(
            "Online tech retailer: refurbished and open-box computers, "
            "components, electronics. Broad tech catalog."
        ),
        build_search_url=lambda q: (
            f"https://www.newegg.ca/p/pl?d={quote_plus(q + ' refurbished')}"
        ),
    ),
    MarketplaceConfig(
        key="openbox_ca",
        display_name="OpenBox.ca",
        description=(
            "Dedicated Canadian open-box/refurb electronics retailer: "
            "MacBooks, laptops, tablets, phones."
        ),
        build_search_url=lambda q: (
            f"https://openbox.ca/search?q={quote_plus(q)}"
        ),
    ),
    MarketplaceConfig(
        key="refurbio",
        display_name="REFURB.io Canada",
        description=(
            "Canadian refurbished computer retailer: Dell, HP, Lenovo, "
            "Samsung laptops and desktops with warranty."
        ),
        build_search_url=lambda q: (
            f"https://ca.refurb.io/search?q={quote_plus(q)}"
        ),
    ),
]

CONFIG_BY_KEY: dict[str, MarketplaceConfig] = {m.key: m for m in CURATED_MARKETPLACES}

# First-exposure eval 2026-07-30 (docs/evals/results.md expand_* rows).
# PARKED, not tuned — re-attempt post-launch:
#   apple_refurbished — browse-only store; category navigation didn't converge
#   dell_refurbished  — search URL template guess likely wrong; verify scheme
# NOTE: bestbuy_outlet was parked from a LOCAL-backend run, then passed on
# Browserbase (841 elems, 5 open-box MacBooks) — backend fingerprint, not a
# bot wall. Retest parked sites on the production backend before believing
# a failure.
_DISABLED_MARKETPLACES: list[MarketplaceConfig] = [
    MarketplaceConfig(
        key="apple_refurbished",
        display_name="Apple Certified Refurbished (Canada)",
        description="Apple refurb store. PARKED: browse-only navigation.",
        build_search_url=lambda q: "https://www.apple.com/ca/shop/refurbished",
    ),
    MarketplaceConfig(
        key="dell_refurbished",
        display_name="Dell Refurbished Canada",
        description="Dell refurb outlet. PARKED: unverified search URL scheme.",
        build_search_url=lambda q: (
            f"https://www.dellrefurbished.ca/search?q={quote_plus(q)}"
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
    local classifieds, laptops -> refurb retailers + classifieds).
  - Retailer refurb/open-box sites ship Canada-wide — ignore any location in
    the query for those; location only matters for local-pickup classifieds.
  - Single-brand outlets (Apple, Dell) only fit queries for that brand.
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
                entry_referer=config.entry_referer,
            ))

        if not targets:
            return self._all_targets(query)
        return targets

    def _all_targets(self, query: str) -> list[MarketplaceSearchTarget]:
        return [
            MarketplaceSearchTarget(
                marketplace=m.key, entry_url=m.build_search_url(query),
                entry_referer=m.entry_referer,
            )
            for m in self.marketplaces
        ]
