"""Community deal sources: RSS feeds and student-specific deal pages."""
from __future__ import annotations

import json
import logging
from urllib.parse import urlparse

import feedparser
import httpx

from dealbot.llm.base import LLMClient
from dealbot.schemas import DealRaw

logger = logging.getLogger(__name__)

COMMUNITY_RSS_FEEDS = [
    "https://forums.redflagdeals.com/feed/hot-deals/",
    "https://slickdeals.net/newsearch.php?mode=frontpage&searcharea=deals&searchin=first&rss=1",
]

STUDENT_DEAL_SOURCES = [
    "https://www.apple.com/ca_edu_hep/shop",
    "https://www.dell.com/en-ca/shop/dell-advantage/cp/students",
    "https://www.lenovo.com/ca/en/deals/student-discounts/",
    "https://www.samsung.com/ca/offer/student-offer/",
    "https://www.microsoft.com/en-ca/education/students",
    "https://www.bestbuy.ca/en-ca/student-deals",
    "https://www.hp.com/ca-en/shop/offer.aspx?p=student-discount",
]

_RSS_EXTRACT_PROMPT = """\
Parse this deal listing title into structured data.

Common formats:
- "Product Name - $price @ Retailer"
- "Product Name | $price at Retailer"
- "50% off Product Name at Retailer"

Return ONLY valid JSON:
{"title": "product name", "price": 99.99, "listed_price": 149.99, "merchant": "Amazon.ca"}

If the listed_price (original/regular price) is not shown, set it equal to price.
If no clear price is found, return: {"title": null, "price": null}"""

_MULTI_DEAL_EXTRACT_PROMPT = """\
Extract all discounted product listings from this student deal page.

For each product with a clear price, extract:
- title: product name and model
- price: current/student price as float
- listed_price: regular price as float (same as price if not shown)
- merchant: retailer or brand name
- url: product page URL if explicitly shown, else omit

Return ONLY valid JSON:
{"products": [{"title": "...", "price": 0.0, "listed_price": 0.0, "merchant": "...", "url": "..."}]}

If no products with prices are found, return: {"products": []}"""


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return "unknown"


async def _parse_rss_title(llm: LLMClient, title: str, url: str) -> DealRaw | None:
    """Extract price and product name from an RSS entry title using the LLM."""
    messages = [
        {"role": "system", "content": _RSS_EXTRACT_PROMPT},
        {"role": "user", "content": f"Title: {title}\nURL: {url}"},
    ]
    try:
        response = await llm.complete(messages)
        content = (response.content or "").strip()
        if content.startswith("```"):
            content = content.split("```")[1].lstrip("json").strip()
        data = json.loads(content)
        if not data.get("title") or not data.get("price"):
            return None
        price = float(data["price"])
        if price <= 0:
            return None
        listed = float(data.get("listed_price") or price)
        if listed < price:
            listed = price
        return DealRaw(
            source=data.get("merchant") or _domain(url),
            title=str(data["title"]).strip(),
            url=url,
            listed_price=listed,
            sale_price=price,
            source_type="scraped",
        )
    except Exception:
        return None


async def fetch_rss_deals(llm: LLMClient, feed_url: str) -> list[DealRaw]:
    """Fetch an RSS feed and extract deals from entry titles. No browser needed."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                feed_url,
                headers={"User-Agent": "Mozilla/5.0 (compatible; DealBot/1.0)"},
                follow_redirects=True,
            )
            resp.raise_for_status()
        feed = feedparser.parse(resp.text)
    except Exception as exc:
        logger.warning("fetch_rss_deals: failed to fetch %s: %s", feed_url, exc)
        return []

    entries = feed.entries[:10]
    logger.info("fetch_rss_deals: %d entries from %s", len(entries), feed_url)

    deals: list[DealRaw] = []
    for entry in entries:
        try:
            title = entry.get("title", "").strip()
            link = entry.get("link", "")
            if not title or not link:
                continue
            deal = await _parse_rss_title(llm, title, link)
            if deal:
                deals.append(deal)
        except Exception:
            continue

    logger.info("fetch_rss_deals: extracted %d deals from %s", len(deals), feed_url)
    return deals


async def fetch_site_deals(llm: LLMClient, url: str) -> list[DealRaw]:
    """Fetch a student deal site and extract multiple deals using the LLM."""
    from dealbot.scrapers.browser_agent import fetch_page

    text = await fetch_page(url)
    if not text:
        logger.warning("fetch_site_deals: empty page for %s", url)
        return []

    messages = [
        {"role": "system", "content": _MULTI_DEAL_EXTRACT_PROMPT},
        {"role": "user", "content": text[:6_000]},
    ]
    try:
        response = await llm.complete(messages)
        content = (response.content or "").strip()
        if content.startswith("```"):
            content = content.split("```")[1].lstrip("json").strip()
        data = json.loads(content)
        deals: list[DealRaw] = []
        for p in data.get("products", []):
            price = float(p.get("price") or 0)
            if price <= 0:
                continue
            listed = float(p.get("listed_price") or price)
            if listed < price:
                listed = price
            title = str(p.get("title") or "").strip()
            if not title:
                continue
            deals.append(DealRaw(
                source=p.get("merchant") or _domain(url),
                title=title,
                url=p.get("url") or url,
                listed_price=listed,
                sale_price=price,
                student_eligible=True,
                source_type="scraped",
            ))
        logger.info("fetch_site_deals: extracted %d deals from %s", len(deals), url)
        return deals
    except Exception as exc:
        logger.warning("fetch_site_deals: extraction failed for %s: %s", url, exc)
        return []
