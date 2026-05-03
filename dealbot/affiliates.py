"""Affiliate URL rewriting — appends tracking params per retailer domain."""
from __future__ import annotations

import os
from urllib.parse import urlparse


def rewrite(url: str | None) -> str | None:
    """Return an affiliate-tagged version of url, or url unchanged if no rule matches."""
    if not url:
        return url

    domain = urlparse(url).netloc.lower()

    if "amazon.ca" in domain or "amazon.com" in domain:
        return _rewrite_amazon(url)
    if "ebay.ca" in domain or "ebay.com" in domain:
        return _rewrite_ebay(url)
    if "bestbuy.ca" in domain:
        return _rewrite_bestbuy(url)

    return url


def _rewrite_amazon(url: str) -> str:
    tag = os.environ.get("AMAZON_ASSOCIATE_TAG", "")
    if not tag:
        return url
    parsed = urlparse(url)
    parts = parsed.path.strip("/").split("/")
    if "dp" in parts:
        idx = parts.index("dp")
        if idx + 1 < len(parts):
            asin = parts[idx + 1]
            return f"https://www.amazon.ca/dp/{asin}?tag={tag}"
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}tag={tag}"


def _rewrite_ebay(url: str) -> str:
    campaign_id = os.environ.get("EPN_CAMPAIGN_ID", "")
    if not campaign_id:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}mkcid=1&mkrid=706-53473-19255-0&campid={campaign_id}&toolid=10001"


def _rewrite_bestbuy(url: str) -> str:
    affiliate_id = os.environ.get("BESTBUY_CA_AFFILIATE_ID", "")
    if not affiliate_id:
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}icid={affiliate_id}"
