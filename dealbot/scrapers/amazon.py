"""Amazon Product Advertising API 5.0 scraper (Canada marketplace)."""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

import httpx

from dealbot.schemas import Condition, DealRaw

logger = logging.getLogger(__name__)

_ENDPOINT = "webservices.amazon.ca"
_URI = "/paapi5/searchitems"
_REGION = "us-east-1"
_SERVICE = "ProductAdvertisingAPI"


def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signature_key(secret: str, date_str: str) -> bytes:
    k_date = _hmac_sha256(f"AWS4{secret}".encode(), date_str)
    k_region = _hmac_sha256(k_date, _REGION)
    k_service = _hmac_sha256(k_region, _SERVICE)
    return _hmac_sha256(k_service, "aws4_request")


async def search_amazon(keyword: str, limit: int = 10) -> list[DealRaw]:
    access_key = os.environ.get("AMAZON_ACCESS_KEY")
    secret_key = os.environ.get("AMAZON_SECRET_KEY")
    associate_tag = os.environ.get("AMAZON_ASSOCIATE_TAG")
    if not all([access_key, secret_key, associate_tag]):
        return []

    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y%m%d")
    datetime_str = now.strftime("%Y%m%dT%H%M%SZ")

    payload = json.dumps({
        "Keywords": keyword,
        "Resources": [
            "ItemInfo.Title",
            "Offers.Listings.Price",
            "Offers.Listings.Condition",
            "Offers.Summaries.HighestPrice",
        ],
        "SearchIndex": "All",
        "PartnerTag": associate_tag,
        "PartnerType": "Associates",
        "Marketplace": "www.amazon.ca",
        "ItemCount": min(limit, 10),
    }, separators=(",", ":"))

    payload_hash = hashlib.sha256(payload.encode()).hexdigest()

    canonical_headers_map = {
        "content-encoding": "amz-1.0",
        "content-type": "application/json; charset=utf-8",
        "host": _ENDPOINT,
        "x-amz-date": datetime_str,
        "x-amz-target": "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.SearchItems",
    }
    signed_headers = ";".join(sorted(canonical_headers_map))
    canonical_headers = "".join(f"{k}:{v}\n" for k, v in sorted(canonical_headers_map.items()))

    canonical_request = "\n".join([
        "POST", _URI, "",
        canonical_headers, signed_headers, payload_hash,
    ])

    credential_scope = f"{date_str}/{_REGION}/{_SERVICE}/aws4_request"
    string_to_sign = "\n".join([
        "AWS4-HMAC-SHA256", datetime_str, credential_scope,
        hashlib.sha256(canonical_request.encode()).hexdigest(),
    ])

    sig = hmac.new(
        _signature_key(secret_key, date_str),
        string_to_sign.encode(),
        hashlib.sha256,
    ).hexdigest()

    headers = {**canonical_headers_map, "Authorization": (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{credential_scope}, "
        f"SignedHeaders={signed_headers}, Signature={sig}"
    )}

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.post(f"https://{_ENDPOINT}{_URI}", content=payload, headers=headers)
            resp.raise_for_status()
        return _parse_items(resp.json(), associate_tag)
    except Exception as exc:
        logger.warning("search_amazon: failed for %r: %s", keyword, exc)
        return []


def _parse_items(data: dict[str, Any], associate_tag: str) -> list[DealRaw]:
    deals = []
    for item in data.get("SearchResult", {}).get("Items", []):
        try:
            title = item["ItemInfo"]["Title"]["DisplayValue"]
            asin = item["ASIN"]
            listings = item.get("Offers", {}).get("Listings", [])
            if not listings:
                continue
            sale_price = float(listings[0]["Price"]["Amount"])
            if sale_price <= 0:
                continue

            listed_price = sale_price
            for s in item.get("Offers", {}).get("Summaries", []):
                hp = s.get("HighestPrice", {}).get("Amount")
                if hp and float(hp) > sale_price:
                    listed_price = float(hp)
                    break

            cond_str = listings[0].get("Condition", {}).get("Value", "New").lower()
            if "used" in cond_str:
                condition = Condition.used
            elif "refurb" in cond_str or "renewed" in cond_str:
                condition = Condition.refurb
            else:
                condition = Condition.new

            deals.append(DealRaw(
                source="Amazon CA",
                title=title,
                url=f"https://www.amazon.ca/dp/{asin}?tag={associate_tag}",
                asin=asin,
                listed_price=listed_price,
                sale_price=sale_price,
                condition=condition,
            ))
        except (KeyError, TypeError, ValueError):
            continue
    return deals
