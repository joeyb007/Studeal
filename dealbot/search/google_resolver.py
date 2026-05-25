"""Tier 1 link resolution for Serper's Google Shopping aggregator URLs.

Strategy:
  - Fetch the Google Shopping product page via httpx (free)
  - Parse aria-label attributes (Google's accessibility convention) with regex
  - Extract Was/Current price and discount percentage deterministically
  - Optionally extract direct retailer URLs from <a href> tags (when present in static HTML)

No LLM. No third-party scraping API. Just HTTP + regex.

Tier 2 (Browserbase + Playwright) is deferred until Tier 1 coverage is measured.
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlparse

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

# Realistic UAs — rotated per call to reduce fingerprint-based throttling
_USER_AGENTS = [
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
]

# Regex against aria-label conventions — much more stable than CSS classes
_RE_CURRENT_PRICE = re.compile(r"Current Price:\s*\$([0-9]+(?:[,.][0-9]{1,2})?)", re.IGNORECASE)
_RE_WAS_PRICE = re.compile(r"Was\s*\$([0-9]+(?:[,.][0-9]{1,2})?)", re.IGNORECASE)
_RE_PERCENT_OFF = re.compile(r"([0-9]+)%\s*OFF", re.IGNORECASE)
# Direct retailer href patterns (we know these stable Canadian retailers from prior data)
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
)
def _parse_money(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


@dataclass
class ResolvedOffer:
    sale_price: float | None = None
    listed_price: float | None = None
    real_discount_pct: float | None = None
    direct_url: str | None = None
    success: bool = False
    failure_reason: str | None = None


@dataclass
class ResolutionStats:
    """Per-query summary of Tier 1 resolution outcomes."""

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
    """Resolves Google Shopping aggregator URLs to extract Was/Is prices.

    Tier 1: pure httpx + regex. Free per call. ~500ms typical latency.
    Bounded concurrency (semaphore) + UA rotation + jitter + retry-once.
    """

    def __init__(self, concurrency: int = 3, timeout: float = 10.0) -> None:
        self._sem = asyncio.Semaphore(concurrency)
        self._timeout = timeout

    async def resolve(self, google_url: str) -> ResolvedOffer:
        async with self._sem:
            # jitter before request to spread load
            await asyncio.sleep(random.uniform(0.3, 1.0))
            async with httpx.AsyncClient(timeout=self._timeout, follow_redirects=True) as client:
                return await self._fetch_and_parse(client, google_url)

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

        return await self._parse_html(client, resp.text)

    async def _parse_html(self, client: httpx.AsyncClient, html: str) -> ResolvedOffer:
        """Parse Google's primary offer block as a unit so prices + URL are correlated.

        Strategy: locate <div data-attrid="apg-product-result">, extract aria-label
        for prices, walk to its parent <a> for the click target, then resolve any
        Google redirect to a direct retailer URL.
        """
        soup = BeautifulSoup(html, "html.parser")
        offer_div = soup.find("div", attrs={"data-attrid": "apg-product-result"})
        if offer_div is None:
            return ResolvedOffer(failure_reason="no_primary_offer")

        aria = offer_div.get("aria-label", "") or ""

        current_match = _RE_CURRENT_PRICE.search(aria)
        was_match = _RE_WAS_PRICE.search(aria)
        pct_match = _RE_PERCENT_OFF.search(aria)

        sale_price = _parse_money(current_match.group(1)) if current_match else None
        listed_price = _parse_money(was_match.group(1)) if was_match else None
        discount_pct = float(pct_match.group(1)) if pct_match else None

        if discount_pct is None and sale_price and listed_price and listed_price > sale_price:
            discount_pct = round((listed_price - sale_price) / listed_price * 100, 1)

        # Find the click-target anchor — it's nested inside the offer_div, not a parent
        direct_url: str | None = None
        anchor = offer_div.find("a", href=True)
        if anchor and anchor.get("href"):
            direct_url = await self._resolve_href(client, anchor["href"])

        # Successful if listed_price is recovered (the field Serper doesn't give us)
        success = listed_price is not None

        return ResolvedOffer(
            sale_price=sale_price,
            listed_price=listed_price,
            real_discount_pct=discount_pct,
            direct_url=direct_url,
            success=success,
            failure_reason=None if success else "no_match",
        )

    async def _resolve_href(self, client: httpx.AsyncClient, href: str) -> str | None:
        """Resolve an anchor href to a direct retailer URL.

        Four cases handled:
        - Direct retailer URL (matches known domain) → return as-is
        - Google /url?q=... wrapper → extract q param
        - Google /aclk?... tracking URL → follow with HEAD, return final location
        - Google /search?ibp=oshop&...headlineOfferDocid:... nested offer page →
          fetch it and find the first direct retailer href inside
        """
        if not href:
            return None

        # Direct retailer URL on a known domain
        for domain in _RETAILER_DOMAINS:
            if domain in href and href.startswith("http"):
                return href

        # Google /url?q=... wrapper
        if href.startswith("/url?") or "google.com/url?" in href:
            try:
                qs = parse_qs(urlparse(href).query)
                q = qs.get("q", [None])[0]
                if q and q.startswith("http"):
                    return q
            except Exception:
                pass
            return None

        full_url = href if href.startswith("http") else f"https://www.google.com{href}"

        # Google /aclk?... tracking redirect — HEAD follow
        if "google.com/aclk" in full_url:
            try:
                resp = await client.head(
                    full_url,
                    headers={"User-Agent": random.choice(_USER_AGENTS)},
                    follow_redirects=True,
                    timeout=5.0,
                )
                return str(resp.url)
            except Exception as exc:
                logger.debug("google_resolver: aclk follow failed: %s", exc)
                return None

        # Nested Google offer page (ibp=oshop with headlineOfferDocid) —
        # fetch HTML and pick the first retailer-domain anchor inside
        if "ibp=oshop" in full_url or "/search?" in full_url:
            try:
                resp = await client.get(
                    full_url,
                    headers={
                        "User-Agent": random.choice(_USER_AGENTS),
                        "Accept-Language": "en-CA,en;q=0.9",
                    },
                    timeout=self._timeout,
                )
                if resp.status_code != 200:
                    return None
                nested = BeautifulSoup(resp.text, "html.parser")
                for a in nested.find_all("a", href=True):
                    h = a["href"]
                    if not h.startswith("http"):
                        continue
                    for domain in _RETAILER_DOMAINS:
                        if domain in h:
                            return h
            except Exception as exc:
                logger.debug("google_resolver: nested fetch failed: %s", exc)
            return None

        return None
