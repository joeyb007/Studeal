"""Tier 1 link resolution for Serper's Google Shopping aggregator URLs.

Two-page fetch strategy (both HTTP + regex, no LLM, no third-party API):

  Page 1: Google Shopping product comparison page (the Serper link)
    → Finds <div data-attrid="apg-product-result"> — the headline offer block
    → Extracts sale_price, pct_off, condition from aria-label
    → Gets the inner <a href> pointing to the nested offer page

  Page 2: Nested Google offer page (headlineOfferDocid URL)
    → Same apg-product-result parsing — sometimes adds Was $X not on page 1
    → First direct retailer <a href> on this page is the click target

Merges best available data from both pages. Handles:
  Type A  — Has Was $X → full strikethrough + discount
  Type A2 — Has X% OFF but no Was → derive listed_price from percentage
  Type B  — Just current price + condition → no discount, valid deal

Success = direct_url is not None (a working retailer link was found).
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

_RE_CURRENT_PRICE = re.compile(r"Current Price:\s*\$([0-9]+(?:[,.][0-9]{1,2})?)", re.IGNORECASE)
_RE_WAS_PRICE     = re.compile(r"Was\s*\$([0-9]+(?:[,.][0-9]{1,2})?)", re.IGNORECASE)
_RE_PERCENT_OFF   = re.compile(r"([0-9]+)%\s*OFF", re.IGNORECASE)
# Condition appears as "Refurbished." / "Pre-owned." after Current Price in aria-label
_RE_CONDITION     = re.compile(
    r"Current Price:\s*\$[\d,]+(?:\.\d{1,2})?\.+\s*(Refurbished|Pre-owned|Open Box|Used|Renewed|As Is|Certified Refurbished)",
    re.IGNORECASE,
)

_RETAILER_DOMAINS = (
    "amazon.ca", "amazon.com",
    "bestbuy.ca", "bestbuy.com",
    "walmart.ca", "walmart.com",
    "apple.com",
    "shoppersdrugmart.ca", "pharmaprix.ca",
    "costco.ca", "costco.com",
    "staples.ca", "staples.com",
    "newegg.ca", "newegg.com",
    "memoryexpress.com",
    "canadacomputers.com",
    "thesource.ca",
    "londondrugs.com",
    "canadiantire.ca",
    "ebay.ca", "ebay.com",
    "poshmark.ca", "poshmark.com",
    "kijiji.ca",
)


def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _extract_offer_data(aria: str) -> dict:
    """Parse all deal signals from a single aria-label string."""
    sale_price   = _parse_money(m.group(1)) if (m := _RE_CURRENT_PRICE.search(aria)) else None
    listed_price = _parse_money(m.group(1)) if (m := _RE_WAS_PRICE.search(aria))     else None
    pct_off      = float(m.group(1))        if (m := _RE_PERCENT_OFF.search(aria))    else None
    condition    = m.group(1).lower()       if (m := _RE_CONDITION.search(aria))      else None

    # Derive listed_price from percentage when Was $X is absent (Type A2)
    if listed_price is None and pct_off and sale_price:
        pct = min(pct_off, 99.0)  # guard against 100%+ nonsense
        listed_price = round(sale_price / (1 - pct / 100), 2)

    # Compute final discount_pct
    discount_pct = None
    if sale_price and listed_price and listed_price > sale_price:
        discount_pct = round((listed_price - sale_price) / listed_price * 100, 1)
    elif pct_off:
        discount_pct = pct_off

    return {
        "sale_price": sale_price,
        "listed_price": listed_price,
        "discount_pct": discount_pct,
        "condition": condition,
    }


@dataclass
class ResolvedOffer:
    sale_price: float | None = None
    listed_price: float | None = None
    real_discount_pct: float | None = None
    condition: str | None = None   # "refurbished" | "pre-owned" | "open box" | "used" | None
    direct_url: str | None = None
    success: bool = False
    failure_reason: str | None = None


@dataclass
class ResolutionStats:
    attempted: int = 0
    succeeded: int = 0
    parse_failed: int = 0
    http_errors: int = 0
    rate_limited: int = 0

    def record(self, offer: ResolvedOffer) -> None:
        self.attempted += 1
        if offer.success:
            self.succeeded += 1
        elif offer.failure_reason == "rate_limited":
            self.rate_limited += 1
        elif offer.failure_reason in ("http_error", "timeout"):
            self.http_errors += 1
        else:
            self.parse_failed += 1

    @property
    def success_rate(self) -> float:
        return self.succeeded / self.attempted if self.attempted else 0.0


class GoogleShoppingResolver:
    """Resolves Google Shopping aggregator URLs via two-page fetch + regex parsing."""

    def __init__(self, concurrency: int = 3, timeout: float = 12.0) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout

    async def resolve(self, google_url: str) -> ResolvedOffer:
        async with self._sem:
            await asyncio.sleep(random.uniform(0.3, 1.0))
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                tier1 = await self._fetch_and_parse(client, google_url)

        if tier1.direct_url:
            return tier1

        # Tier 2: Browserbase + aria snapshot
        logger.info("google_resolver: Tier 1 failed (%s), escalating to Tier 2 for %s",
                    tier1.failure_reason, google_url[:60])
        return await self._resolve_tier2(google_url)

    async def _resolve_tier2(self, google_url: str) -> ResolvedOffer:
        """Tier 2: render via Browserbase with residential proxies, extract from aria."""
        from dealbot.scrapers.browser_agent import resolve_serper_url
        offer = await resolve_serper_url(google_url)
        if offer is None or not offer.url:
            return ResolvedOffer(failure_reason="tier2_no_offer")
        return ResolvedOffer(
            sale_price=offer.sale_price,
            listed_price=offer.listed_price,
            real_discount_pct=offer.discount_pct,
            condition=offer.condition,
            direct_url=offer.url,
            success=True,
            failure_reason=None,
        )

    async def _fetch_and_parse(
        self, client: httpx.AsyncClient, url: str, _attempt: int = 0,
    ) -> ResolvedOffer:
        headers = {
            "User-Agent": random.choice(_USER_AGENTS),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-CA,en;q=0.9",
        }
        try:
            resp = await client.get(url, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.debug("google_resolver: http error: %s", exc)
            return ResolvedOffer(failure_reason="timeout")

        if resp.status_code == 429:
            if _attempt == 0:
                await asyncio.sleep(2.0)
                return await self._fetch_and_parse(client, url, _attempt=1)
            return ResolvedOffer(failure_reason="rate_limited")

        if resp.status_code >= 400:
            return ResolvedOffer(failure_reason="http_error")

        return await self._parse_pages(client, resp.text)

    async def _parse_pages(self, client: httpx.AsyncClient, page1_html: str) -> ResolvedOffer:
        """Parse Page 1 for headline offer data, then fetch Page 2 for the
        direct retailer URL and any additional price data."""

        soup1 = BeautifulSoup(page1_html, "html.parser")
        offer_div = soup1.find("div", attrs={"data-attrid": "apg-product-result"})

        if offer_div is None:
            # Fallback: organic_offers_grid — layout used when there's no featured
            # comparison offer (e.g., full-price/new products). Has direct retailer
            # URLs and prices but no Was/discount data.
            return self._parse_offers_grid(soup1)

        # Extract initial offer data from Page 1 aria-label
        p1 = _extract_offer_data(offer_div.get("aria-label", "") or "")

        # Get inner anchor for Page 2
        inner_a = offer_div.find("a", href=True)
        if not inner_a:
            return ResolvedOffer(failure_reason="no_inner_anchor")

        href = inner_a["href"]
        full_href = href if href.startswith("http") else f"https://www.google.com{href}"

        # Fetch Page 2 — the nested headlineOfferDocid page
        direct_url, p2 = await self._fetch_nested(client, full_href)

        # If nested page gave no direct URL, fall back to organic_offers_grid on page 1
        if not direct_url:
            grid_result = self._parse_offers_grid(soup1)
            if grid_result.direct_url:
                direct_url = grid_result.direct_url
                # Use grid price data only if nested page gave us nothing
                if not p2.get("sale_price"):
                    p2["sale_price"] = grid_result.sale_price

        # Merge: Page 2 price data takes priority (more specific to this offer)
        sale_price    = p2.get("sale_price")    or p1.get("sale_price")
        listed_price  = p2.get("listed_price")  or p1.get("listed_price")
        discount_pct  = p2.get("discount_pct")  or p1.get("discount_pct")
        condition     = p2.get("condition")      or p1.get("condition")

        # Sanity check: listed_price must be >= sale_price, otherwise it's cross-page noise
        if listed_price is not None and sale_price is not None and listed_price < sale_price:
            listed_price = None
            discount_pct = None

        # Recompute discount if merge produced both prices
        if sale_price and listed_price and listed_price > sale_price and not discount_pct:
            discount_pct = round((listed_price - sale_price) / listed_price * 100, 1)

        success = direct_url is not None

        return ResolvedOffer(
            sale_price=sale_price,
            listed_price=listed_price,
            real_discount_pct=discount_pct,
            condition=condition,
            direct_url=direct_url,
            success=success,
            failure_reason=None if success else "no_direct_url",
        )

    def _parse_offers_grid(self, soup: BeautifulSoup) -> ResolvedOffer:
        """Fallback for pages without apg-product-result.

        organic_offers_grid contains a list of retailer offers with direct URLs
        and prices. We take the cheapest offer with a known retailer URL.
        No Was/discount data available — this is always Type B (sale_price only).
        """
        grid = soup.find("div", attrs={"data-attrid": "organic_offers_grid"})
        if grid is None:
            return ResolvedOffer(failure_reason="no_offer_layout")

        # Find cheapest price from aria-labels in the grid
        best_price: float | None = None
        for el in grid.find_all(attrs={"aria-label": True}):
            aria = el.get("aria-label", "")
            m = _RE_CURRENT_PRICE.search(aria)
            if m:
                price = _parse_money(m.group(1))
                if price and (best_price is None or price < best_price):
                    best_price = price

        # Find first direct retailer URL in the grid
        direct_url: str | None = None
        for a in grid.find_all("a", href=True):
            h = a["href"]
            if not h.startswith("http"):
                continue
            if any(domain in h for domain in _RETAILER_DOMAINS):
                direct_url = h
                break

        success = direct_url is not None
        return ResolvedOffer(
            sale_price=best_price,
            listed_price=None,
            real_discount_pct=None,
            condition=None,
            direct_url=direct_url,
            success=success,
            failure_reason=None if success else "no_direct_url_in_grid",
        )

    async def _fetch_nested(
        self, client: httpx.AsyncClient, url: str,
    ) -> tuple[str | None, dict]:
        """Fetch the nested Google offer page.

        Returns:
          (direct_retailer_url, price_data_dict)
        Price data dict may be empty if the nested page yielded nothing useful.
        """
        # Handle direct retailer URL (no nested fetch needed)
        for domain in _RETAILER_DOMAINS:
            if domain in url and url.startswith("http"):
                return url, {}

        # Google /url?q= wrapper
        if "/url?" in url:
            try:
                qs = parse_qs(urlparse(url).query)
                q = qs.get("q", [None])[0]
                if q and q.startswith("http"):
                    return q, {}
            except Exception:
                pass
            return None, {}

        # Google /aclk redirect — follow to final destination
        if "google.com/aclk" in url:
            try:
                resp = await client.head(url, headers={"User-Agent": random.choice(_USER_AGENTS)},
                                         follow_redirects=True, timeout=5.0)
                return str(resp.url), {}
            except Exception as exc:
                logger.debug("google_resolver: aclk follow failed: %s", exc)
            return None, {}

        # Nested Google offer page — fetch and parse
        if "ibp=oshop" in url or "/search?" in url:
            try:
                resp = await client.get(
                    url,
                    headers={
                        "User-Agent": random.choice(_USER_AGENTS),
                        "Accept-Language": "en-CA,en;q=0.9",
                    },
                    timeout=self._timeout,
                )
                if resp.status_code != 200:
                    return None, {}

                nested = BeautifulSoup(resp.text, "html.parser")

                # Extract price data from the nested page's headline offer
                nested_offer = nested.find("div", attrs={"data-attrid": "apg-product-result"})
                price_data = _extract_offer_data(nested_offer.get("aria-label", "") or "") \
                    if nested_offer else {}

                # First direct retailer URL on the nested page
                direct_url = None
                for a in nested.find_all("a", href=True):
                    h = a["href"]
                    if not h.startswith("http"):
                        continue
                    if any(domain in h for domain in _RETAILER_DOMAINS):
                        direct_url = h
                        break

                return direct_url, price_data

            except Exception as exc:
                logger.debug("google_resolver: nested fetch failed: %s", exc)
            return None, {}

        return None, {}
