"""On-demand single-listing fetch (copilot spec 2026-08-10, phase D).

A pasted URL Scout has never seen gets one browser visit: snapshot the page,
extract the listing's facts with a cheap JSON call, persist it through the
same upsert path hunts use (embedding included), and hand back the row. Any
failure returns None — the caller offers a retry, never a stack trace.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from dealbot.agents.marketplace_router import CONFIG_BY_KEY
from dealbot.agents.workers.extractor import Offer
from dealbot.db.models import Listing
from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

FETCHABLE_MARKETPLACES = ("kijiji", "ebay", "fb_marketplace")

FETCH_EXTRACT_SYSTEM = """Extract the single marketplace listing from this page text.

Output JSON:
{"available": bool, "title": str, "price": number, "currency": str,
 "condition": "new"|"refurbished"|"used"|"unknown", "location": str|null}

- available: false when the page says the listing is gone, sold, or
  unavailable, or when this is not a single listing's page at all.
- price: the asking price as a number. Genuinely absent means available=false.
- currency: the listed currency code (CAD, USD, ...). Infer from the site's
  country only when the page shows a bare $.
- Never invent values. JSON only."""


def _extract_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_NAV_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


async def _visit_url(url: str, marketplace: str) -> tuple[str, str | None] | None:
    """(snapshot_text, og_image_url) for a listing page; None on navigation
    failure. Same session + referer discipline as the inspector's visit."""
    from dealbot.agents.composition import build_session_from_env
    from dealbot.agents.explorer import _settled_snapshot

    cfg = CONFIG_BY_KEY.get(marketplace)
    goto_kwargs: dict[str, Any] = {}
    if cfg is not None and cfg.entry_referer:
        goto_kwargs["referer"] = cfg.entry_referer

    try:
        async with build_session_from_env() as session:
            page = session.page
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000, **goto_kwargs)
            snap = await _settled_snapshot(page)
            og_image = await page.evaluate(
                "() => document.querySelector('meta[property=\"og:image\"]')?.content ?? null"
            )
            return snap.text, og_image if isinstance(og_image, str) and og_image else None
    except Exception as exc:
        logger.warning("fetch_listing: visit failed for %s: %s", url, exc)
        return None


async def fetch_and_persist(url: str, marketplace: str) -> Listing | None:
    """Visit, extract, upsert. None on any failure or an unavailable page."""
    from dealbot.db.database import get_async_session
    from dealbot.persistence.canonicalize import canonicalize_url
    from dealbot.persistence.listings import persist_offers

    if marketplace not in FETCHABLE_MARKETPLACES:
        return None

    visited = await _visit_url(url, marketplace)
    if visited is None:
        return None
    snapshot_text, og_image = visited

    try:
        response = await _extract_llm().complete(
            [
                {"role": "system", "content": FETCH_EXTRACT_SYSTEM},
                {"role": "user", "content": snapshot_text[:12_000]},
            ],
            response_format={"type": "json_object"},
        )
        data = json.loads(response.content or "{}")
    except Exception:
        logger.warning("fetch_listing: extraction failed for %s", url, exc_info=True)
        return None

    title = str(data.get("title") or "").strip()
    price = data.get("price")
    if not data.get("available") or not title or not isinstance(price, (int, float)) or price <= 0:
        return None

    condition = data.get("condition")
    offer = Offer(
        title=title[:300],
        price=float(price),
        currency=str(data.get("currency") or "CAD")[:8],
        url=url,
        image_url=og_image,
        location=(str(data["location"])[:200] if data.get("location") else None),
        condition=condition if condition in ("new", "refurbished", "used") else "unknown",
        marketplace=marketplace,
    )
    result = await persist_offers([offer])
    if result.written == 0:
        return None

    canonical = canonicalize_url(url, marketplace)
    async with get_async_session() as session:
        from sqlalchemy import select

        return (await session.execute(
            select(Listing).where(Listing.canonical_url == canonical).limit(1)
        )).scalars().first()
