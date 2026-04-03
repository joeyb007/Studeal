from __future__ import annotations

import logging
import re
from typing import Optional

import feedparser
import httpx

from dealbot.scrapers.base import BaseAdapter
from dealbot.schemas import DealRaw

logger = logging.getLogger(__name__)

SLICKDEALS_RSS = "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1"

# Matches prices like $49.99 or $1,299.00
_PRICE_RE = re.compile(r"\$[\d,]+(?:\.\d{2})?")


def _parse_price(text: str) -> Optional[float]:
    """Extract the first dollar amount from a string."""
    match = _PRICE_RE.search(text)
    if not match:
        return None
    return float(match.group().replace("$", "").replace(",", ""))


def _extract_prices(entry: feedparser.FeedParserDict) -> tuple[float, float]:
    """
    Attempt to extract listed and sale prices from an RSS entry.
    Slickdeals doesn't give structured price fields, so we parse the title and summary.

    Returns (listed_price, sale_price). If only one price is found, both are set to it
    and the scorer will treat the discount as unverified.
    """
    text = f"{entry.get('title', '')} {entry.get('summary', '')}"
    prices = []

    for match in _PRICE_RE.finditer(text):
        val = float(match.group().replace("$", "").replace(",", ""))
        prices.append(val)

    if len(prices) >= 2:
        # Assume higher = listed (original), lower = sale
        listed = max(prices[:2])
        sale = min(prices[:2])
        return listed, sale
    elif len(prices) == 1:
        return prices[0], prices[0]
    else:
        # No price found — use 0.0 as sentinel; scorer will flag low confidence
        return 0.0, 0.0


def _normalise_entry(entry: feedparser.FeedParserDict) -> Optional[DealRaw]:
    """Convert a single RSS entry into a DealRaw. Returns None if entry is unusable."""
    title = entry.get("title", "").strip()
    url = entry.get("link", "").strip()

    if not title or not url:
        logger.warning("slickdeals: skipping entry with missing title or url")
        return None

    listed_price, sale_price = _extract_prices(entry)

    return DealRaw(
        source="slickdeals",
        title=title,
        url=url,
        listed_price=listed_price,
        sale_price=sale_price,
        asin=None,  # Slickdeals RSS doesn't include ASINs
        description=entry.get("summary", "").strip() or None,
    )


class SlickdealsAdapter(BaseAdapter):
    def __init__(self, feed_url: str = SLICKDEALS_RSS, timeout: int = 15) -> None:
        self._feed_url = feed_url
        self._timeout = timeout

    async def fetch(self) -> list[DealRaw]:
        logger.info("slickdeals: fetching feed %s", self._feed_url)

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            resp = await client.get(self._feed_url)
            resp.raise_for_status()
            raw_xml = resp.text

        feed = feedparser.parse(raw_xml)

        if feed.bozo:
            logger.warning("slickdeals: feed parse warning: %s", feed.bozo_exception)

        deals: list[DealRaw] = []
        for entry in feed.entries:
            deal = _normalise_entry(entry)
            if deal is not None:
                deals.append(deal)

        logger.info("slickdeals: parsed %d deals", len(deals))
        return deals
