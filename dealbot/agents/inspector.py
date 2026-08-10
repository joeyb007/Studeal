"""Deal Inspector Tier A: Scout's objective look at one listing.

"Send it to Scout": visit the real listing page with the hunt browser stack,
read it the way a person would (rendered screenshots + extracted text), and
produce a cached, shared report. The personal verdict (Tier B) is layered on
elsewhere; nothing in this module depends on who asked.

Trust boundaries:
  - Photos reach the model as rendered-page screenshots. No per-site gallery
    scraping, ever (zero-adapter rule).
  - Comps and the price band are computed here, deterministically, and
    attached to the report after the LLM call. The model never invents them.
  - A dead page marks the listing sold (`listings.sold_at`); read surfaces
    exclude it immediately. Navigation FAILURES are not death: only a page
    that renders and says the listing is gone counts.
  - All user-facing text is sanitized (house rule: no em dashes).
"""

from __future__ import annotations

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ValidationError
from sqlalchemy import select

from dealbot.agents.marketplace_router import CONFIG_BY_KEY
from dealbot.agents.playbook import price_band, sanitize
from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, ListingInspection
from dealbot.lifecycle import stale_cutoff
from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_SNAPSHOT_TEXT_BUDGET = 14_000
_FRAME_COUNT = 3
_COMPS_LIMIT = 40
_COMP_STRIP = 3


# ---------------------------------------------------------------------------
# LLM clients
# ---------------------------------------------------------------------------

def _report_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        # The report is Scout's voice + a vision condition read: judgment tier.
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


def _detail_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_EXTRACT_MODEL"))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


# ---------------------------------------------------------------------------
# Detail extraction (Haiku, text only)
# ---------------------------------------------------------------------------

class ListingDetail(BaseModel):
    gone: bool = False                    # page says the listing is unavailable
    description: str | None = None
    seller: str | None = None
    posted_raw: str | None = None         # e.g. "Listed 11 days ago"
    location: str | None = None
    price: float | None = None


DETAIL_SYSTEM = """You read ONE marketplace listing detail page (accessibility-tree
text) and emit JSON with exactly these keys:
{"gone": bool, "description": str|null, "seller": str|null,
 "posted_raw": str|null, "location": str|null, "price": number|null}

"gone" is true ONLY if the page itself says the listing is unavailable, removed,
deleted, sold, or not found. A page showing normal listing content means false.
"description" is the seller's own text, verbatim, trimmed to what matters.
Only report what is actually on the page. Output JSON only."""


async def _extract_detail(snapshot_text: str) -> ListingDetail:
    llm = _detail_llm()
    response = await llm.complete(
        [
            {"role": "system", "content": DETAIL_SYSTEM},
            {"role": "user", "content": snapshot_text[:_SNAPSHOT_TEXT_BUDGET]},
        ],
        response_format={"type": "json_object"},
    )
    try:
        return ListingDetail.model_validate_json(response.content)
    except (ValidationError, TypeError):
        logger.warning("inspector: detail parse failed; treating page as unreadable")
        return ListingDetail()


# ---------------------------------------------------------------------------
# Comps (deterministic)
# ---------------------------------------------------------------------------

async def _comps(listing: Listing) -> list[dict[str, Any]]:
    """Closest live pool neighbors of this listing (excluding itself)."""
    if listing.embedding is None:
        return []
    try:
        async with get_async_session() as session:
            stmt = (
                select(Listing)
                .where(Listing.id != listing.id)
                .where(Listing.last_seen_at >= stale_cutoff())
                .where(Listing.sold_at.is_(None))
                .where(Listing.embedding.isnot(None))
                .order_by(Listing.embedding.cosine_distance(listing.embedding))
                .limit(_COMPS_LIMIT)
            )
            rows = list((await session.execute(stmt)).scalars().all())
        return [
            {"id": r.id, "title": r.title, "price": r.price, "marketplace": r.marketplace}
            for r in rows
        ]
    except Exception:
        logger.warning("inspector: comps query failed for listing %d", listing.id, exc_info=True)
        return []


def _comps_block(listing: Listing, comps: list[dict[str, Any]]) -> str:
    band = price_band([c["price"] for c in comps])
    if band is None:
        return "Market context: not enough similar live listings for a price band."
    p25, p50, p75 = band
    return (
        f"Market context ({len(comps)} similar live listings): "
        f"25th percentile ${p25:.0f}, median ${p50:.0f}, 75th percentile ${p75:.0f}. "
        f"This listing is priced ${listing.price:.0f}."
    )


# ---------------------------------------------------------------------------
# The report (Sonnet + vision)
# ---------------------------------------------------------------------------

REPORT_SYSTEM = """You are Scout, the user's expert friend who knows secondhand
markets cold. You are looking at screenshots of ONE live marketplace listing,
plus its extracted text. Give your honest expert read.

Output JSON with exactly these keys (all strings plain language, second person):
{
  "headline": str,            // one scannable sentence, under 90 chars: your take at a glance
  "condition_grade": "good"|"fair"|"worn"|"unknown",  // overall, from the photos
  "photos_real": true|false|null,  // true: photos show the actual item; false: stock images, renders, or catalog shots only; null: cannot tell
  "identification": str,      // what this actually is: model, generation, variant, confidence
  "condition": str,           // condition read, referencing what you see ("photo 2, left armrest")
  "red_flags": [str],         // real concerns only; [] if none
  "cant_tell": str,           // what a photo inspection cannot verify for this item
  "seller_questions": [str],  // 2-3 specific questions to send before committing
  "legitimacy": {"level": "fine"|"caution"|"likely_scam", "reason": str},
  "market_position": str,     // this price vs the band you were given; use ONLY provided numbers
  "summary": str              // 2-3 sentences, your overall take, no verdict on fit (that is personal)
}

Rules:
- Only claim what the screenshots and text support. Uncertainty goes in cant_tell.
- photos_real is false only when EVERY image looks stock (renders, catalog
  shots, watermarks from other sites). One genuine photo of the item makes it true.
- Legitimacy: price far below the band, stock photos, or off-platform payment asks
  raise it; normal listings are "fine".
- Never use em dashes. No greetings. JSON only."""


def _frame_part(frame: bytes) -> dict[str, Any]:
    b64 = base64.b64encode(frame).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _sanitize_obj(value: Any) -> Any:
    if isinstance(value, str):
        return sanitize(value)
    if isinstance(value, list):
        return [_sanitize_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: _sanitize_obj(v) for k, v in value.items()}
    return value


_REPORT_KEYS = {
    "headline", "condition_grade", "photos_real", "identification", "condition",
    "red_flags", "cant_tell", "seller_questions", "legitimacy", "market_position",
    "summary",
}


def derive_flags(report: dict | None) -> dict | None:
    """Objective, user-independent flags distilled from a report (spec
    2026-08-10): what quality filtering and trust badges read downstream.
    None without a report; individual fields None when the report can't say."""
    if not report:
        return None
    legitimacy = report.get("legitimacy") or {}
    photos_real = report.get("photos_real")
    return {
        "photos_real": photos_real if isinstance(photos_real, bool) else None,
        "condition_grade": report.get("condition_grade"),
        "legit_level": legitimacy.get("level"),
        "legit_reason": legitimacy.get("reason"),
    }

# Within this fraction of the comp median, a price reads as "fair".
_FAIR_BAND_FRACTION = 0.12


def price_read(price: float, comps: list[dict[str, Any]]) -> dict[str, str] | None:
    """Deterministic price-vs-market read for the overview card. The model
    writes prose about the market; this badge is arithmetic, not opinion."""
    band = price_band([c["price"] for c in comps])
    if band is None:
        return None
    _, median, _ = band
    delta = price - median
    if abs(delta) <= _FAIR_BAND_FRACTION * median:
        return {"level": "fair", "text": "fair for the market"}
    if delta > 0:
        return {"level": "over", "text": f"about ${delta:.0f} over the going rate"}
    return {"level": "under", "text": f"about ${-delta:.0f} under the going rate"}


async def _generate_report(
    listing: Listing,
    detail: ListingDetail,
    frames: list[bytes],
    comps: list[dict[str, Any]],
) -> dict[str, Any] | None:
    grounding = "\n".join([
        f"Listing: {listing.title}",
        f"Marketplace: {listing.marketplace} · Price: ${listing.price:.2f} {listing.currency}",
        f"Condition claimed: {listing.condition}",
        f"Seller text: {detail.description or '(none extracted)'}",
        f"Posted: {detail.posted_raw or 'unknown'} · Location: {detail.location or 'unknown'}",
        _comps_block(listing, comps),
    ])
    content: list[dict[str, Any]] = [{"type": "text", "text": grounding}]
    content += [_frame_part(f) for f in frames]

    llm = _report_llm()
    response = await llm.complete(
        [
            {"role": "system", "content": REPORT_SYSTEM},
            {"role": "user", "content": content},
        ],
        response_format={"type": "json_object"},
    )
    try:
        report = json.loads(response.content)
    except (json.JSONDecodeError, TypeError):
        logger.warning("inspector: report parse failed for listing %d", listing.id)
        return None
    if not isinstance(report, dict) or not _REPORT_KEYS.issubset(report):
        logger.warning("inspector: report missing keys for listing %d", listing.id)
        return None
    report = _sanitize_obj(report)
    # Deterministic attachments: the comp strip and price badge are ours,
    # not the model's.
    report["comps"] = comps[:_COMP_STRIP]
    report["price_read"] = price_read(listing.price, comps)
    return report


# ---------------------------------------------------------------------------
# The visit
# ---------------------------------------------------------------------------

async def _visit(listing: Listing) -> tuple[str, list[bytes]] | None:
    """Open the listing page; return (snapshot_text, frames) or None on
    navigation failure. Never raises."""
    from dealbot.agents.composition import build_session_from_env
    from dealbot.agents.explorer import _settled_snapshot

    cfg = CONFIG_BY_KEY.get(listing.marketplace)
    goto_kwargs: dict[str, Any] = {}
    if cfg is not None and cfg.entry_referer:
        goto_kwargs["referer"] = cfg.entry_referer

    try:
        async with build_session_from_env() as session:
            page = session.page
            await page.goto(
                listing.raw_url, wait_until="domcontentloaded", timeout=30_000,
                **goto_kwargs,
            )
            snap = await _settled_snapshot(page)
            frames: list[bytes] = []
            frames.append(await page.screenshot(type="jpeg", quality=55))
            for _ in range(_FRAME_COUNT - 1):
                await page.mouse.wheel(0, 900)
                await page.wait_for_timeout(1_200)
                frames.append(await page.screenshot(type="jpeg", quality=55))
            return snap.text, frames
    except Exception as exc:
        logger.warning("inspector: visit failed for listing %d: %s", listing.id, exc)
        return None


# ---------------------------------------------------------------------------
# Orchestration + cache
# ---------------------------------------------------------------------------

async def _mark_sold(listing_id: int) -> None:
    async with get_async_session() as session:
        row = await session.get(Listing, listing_id)
        if row is not None and row.sold_at is None:
            row.sold_at = datetime.now(timezone.utc)
            await session.commit()


async def _write_cache(listing_id: int, status: str, report: dict | None, detail: ListingDetail) -> None:
    async with get_async_session() as session:
        existing = await session.get(ListingInspection, listing_id)
        if existing is None:
            existing = ListingInspection(listing_id=listing_id)
            session.add(existing)
        existing.status = status
        existing.report = json.dumps(report) if report is not None else None
        existing.detail = detail.model_dump_json()
        existing.flags = derive_flags(report)
        existing.created_at = datetime.now(timezone.utc)
        await session.commit()


def _payload(status: str, *, report: dict | None = None, detail: ListingDetail | None = None,
             comps: list[dict] | None = None, cached: bool = False,
             created_at: str | None = None) -> dict[str, Any]:
    return {
        "status": status,
        "report": report,
        "detail": detail.model_dump() if detail is not None else None,
        "comps": comps or [],
        "cached": cached,
        "created_at": created_at,
    }


async def get_cached_inspection(listing_id: int) -> dict[str, Any] | None:
    async with get_async_session() as session:
        row = await session.get(ListingInspection, listing_id)
    if row is None:
        return None
    report = json.loads(row.report) if row.report else None
    detail = ListingDetail.model_validate_json(row.detail) if row.detail else ListingDetail()
    return _payload(
        row.status, report=report, detail=detail,
        comps=(report or {}).get("comps", []), cached=True,
        created_at=row.created_at.isoformat(),
    )


async def get_or_create_inspection(listing_id: int, force: bool = False) -> dict[str, Any]:
    """The whole Tier A flow. Statuses: ok | listing_gone | error.
    Errors are never cached; gone and ok are."""
    if not force:
        cached = await get_cached_inspection(listing_id)
        if cached is not None:
            return cached

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
    if listing is None:
        return _payload("error")

    comps = await _comps(listing)

    visited = await _visit(listing)
    if visited is None:
        # Transient navigation failure: not evidence the listing is gone.
        return _payload("error", comps=comps[:_COMP_STRIP])
    snapshot_text, frames = visited

    detail = await _extract_detail(snapshot_text)
    if detail.gone:
        await _mark_sold(listing_id)
        await _write_cache(listing_id, "listing_gone", None, detail)
        return _payload("listing_gone", detail=detail, comps=comps[:_COMP_STRIP])

    report = await _generate_report(listing, detail, frames, comps)
    if report is None:
        return _payload("error", detail=detail, comps=comps[:_COMP_STRIP])

    await _write_cache(listing_id, "ok", report, detail)
    return _payload(
        "ok", report=report, detail=detail, comps=report.get("comps", []),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
