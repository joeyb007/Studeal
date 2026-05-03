"""eBay Browse API scraper with Redis-cached OAuth token."""
from __future__ import annotations

import base64
import logging
import os
from typing import Any

import httpx

from dealbot.schemas import Condition, DealRaw

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
_SEARCH_URL = "https://api.ebay.com/buy/browse/v1/item_summary/search"
_REDIS_KEY = "ebay:app_token"

_CONDITION_MAP: dict[str, Condition] = {
    "1000": Condition.new,
    "1500": Condition.new,
    "2000": Condition.refurb,
    "2500": Condition.refurb,
    "3000": Condition.used,
    "4000": Condition.used,
    "5000": Condition.used,
    "6000": Condition.used,
}


async def _get_token() -> str | None:
    app_id = os.environ.get("EBAY_APP_ID")
    client_secret = os.environ.get("EBAY_CLIENT_SECRET")
    if not app_id or not client_secret:
        return None

    redis_url = os.environ.get("REDIS_URL", "redis://localhost:6380/0")
    try:
        import redis as redis_lib
        r = redis_lib.from_url(redis_url, decode_responses=True)
        cached = r.get(_REDIS_KEY)
        if cached:
            return cached
    except Exception:
        pass

    creds = base64.b64encode(f"{app_id}:{client_secret}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                _TOKEN_URL,
                headers={
                    "Authorization": f"Basic {creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={
                    "grant_type": "client_credentials",
                    "scope": "https://api.ebay.com/oauth/api_scope",
                },
            )
            resp.raise_for_status()
        data = resp.json()
        token = data["access_token"]
        ttl = int(data.get("expires_in", 7200)) - 60
        try:
            r.set(_REDIS_KEY, token, ex=ttl)
        except Exception:
            pass
        return token
    except Exception as exc:
        logger.warning("eBay token fetch failed: %s", exc)
        return None


async def search_ebay(keyword: str, limit: int = 10) -> list[DealRaw]:
    token = await _get_token()
    if not token:
        return []

    campaign_id = os.environ.get("EPN_CAMPAIGN_ID", "")
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(
                _SEARCH_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "q": keyword,
                    "limit": min(limit, 20),
                    "filter": "buyingOptions:{FIXED_PRICE}",
                },
            )
            resp.raise_for_status()
        return _parse_items(resp.json(), campaign_id)
    except Exception as exc:
        logger.warning("search_ebay: failed for %r: %s", keyword, exc)
        return []


def _parse_items(data: dict[str, Any], campaign_id: str) -> list[DealRaw]:
    deals = []
    for item in data.get("itemSummaries", []):
        try:
            sale_price = float(item.get("price", {}).get("value", 0))
            if sale_price <= 0:
                continue

            url = item.get("itemWebUrl", "") or ""
            if campaign_id and url:
                sep = "&" if "?" in url else "?"
                url = f"{url}{sep}mkcid=1&mkrid=706-53473-19255-0&campid={campaign_id}&toolid=10001"

            condition = _CONDITION_MAP.get(item.get("conditionId", ""), Condition.unknown)

            deals.append(DealRaw(
                source="eBay",
                title=item["title"],
                url=url or None,
                listed_price=sale_price,  # eBay rarely provides original price
                sale_price=sale_price,
                condition=condition,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return deals
