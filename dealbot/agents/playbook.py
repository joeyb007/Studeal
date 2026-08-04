"""Watchlist playbook: Scout's category expertise, generated once per
watchlist and refreshed on profile edits.

Grounding is computed here, deterministically: price band from pool comps
(intent-embedding cosine neighbors), comp count, marketplace mix. The LLM
writes prose around numbers it is given; it never invents the band.

Failure posture: best-effort everywhere. Any failure returns False and
leaves the row untouched; callers never propagate.
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, Watchlist
from dealbot.lifecycle import stale_cutoff
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

_COMPS_LIMIT = 60
_MIN_COMPS = 5


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        # Scout's voice lives on the judgment-tier model (see watchlists._get_llm).
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    if backend == "openai":
        from dealbot.llm.openai_client import OpenAIClient
        return OpenAIClient()
    from dealbot.llm.ollama import OllamaClient
    return OllamaClient()


def price_band(prices: list[float]) -> tuple[float, float, float] | None:
    """p25 / median / p75 over comp prices; None below _MIN_COMPS (an honest
    'pool still filling' beats a band computed from three listings)."""
    if len(prices) < _MIN_COMPS:
        return None
    qs = statistics.quantiles(prices, n=4)
    return (qs[0], statistics.median(prices), qs[2])


def sanitize(text: str) -> str:
    """Product copy never ships em dashes (house rule): separator form
    becomes the middle dot, joiner form a hyphen."""
    return text.replace(" — ", " · ").replace("—", "-")


PLAYBOOK_SYSTEM = """You are Scout, the user's expert friend who knows secondhand
markets cold. Write their pre-hunt playbook for the category below: the advice a
knowledgeable friend gives BEFORE they start looking at listings.

Write 4 short sections with these exact headings:
What to check
The going rate
How to haggle
Your walk-away

Rules:
- Plain language, second person, specific to THIS category. No generic advice.
- "What to check": the 3-4 real failure points and what good condition actually
  means for this item class.
- "The going rate": use ONLY the price numbers provided in the data block. If no
  band is provided, say the pool is still filling and give honest general
  guidance for the category instead of inventing numbers.
- "How to haggle": norms for the marketplaces listed (local classifieds expect
  offers; fixed-price retail mostly does not), typical discount depth.
- "Your walk-away": concrete numbers from their budget and the band.
- 180-260 words total. Never use em dashes. No greetings, no sign-off."""


def _render_grounding(context: WatchlistContext, comps: list[Listing]) -> str:
    lines = [f"Category: {context.product_query}"]
    if context.max_budget:
        lines.append(f"Buyer budget: ${context.max_budget:.0f}")
    if context.buyer_profile:
        lines.append(f"Buyer profile: {context.buyer_profile}")
    band = price_band([c.price for c in comps])
    if band:
        p25, p50, p75 = band
        marketplaces = sorted({c.marketplace for c in comps})
        lines.append(
            f"Live comps: {len(comps)} similar listings right now. "
            f"Price band: 25th percentile ${p25:.0f}, median ${p50:.0f}, "
            f"75th percentile ${p75:.0f}."
        )
        lines.append(f"Marketplaces they appear on: {', '.join(marketplaces)}")
    else:
        lines.append("Live comps: not enough similar listings in the pool yet for a price band.")
    return "\n".join(lines)


async def _comps(watchlist: Watchlist) -> list[Listing]:
    """Dense neighbors of the watchlist intent; [] on any failure — a missing
    band degrades the playbook, it never blocks it."""
    if watchlist.intent_embedding is None:
        return []
    try:
        async with get_async_session() as session:
            stmt = (
                select(Listing)
                .where(Listing.last_seen_at >= stale_cutoff())
                .where(Listing.sold_at.is_(None))
                .where(Listing.embedding.isnot(None))
                .order_by(Listing.embedding.cosine_distance(watchlist.intent_embedding))
                .limit(_COMPS_LIMIT)
            )
            return list((await session.execute(stmt)).scalars().all())
    except Exception:
        logger.warning("playbook comps query failed for wl %d", watchlist.id, exc_info=True)
        return []


async def generate_playbook(watchlist_id: int) -> bool:
    """Generate and persist the playbook. False on any failure; never raises."""
    try:
        async with get_async_session() as session:
            watchlist = await session.get(Watchlist, watchlist_id)
            if watchlist is None or not watchlist.context:
                return False
            context = WatchlistContext.model_validate_json(watchlist.context)
            comps = await _comps(watchlist)

        grounding = _render_grounding(context, comps)
        llm = _get_llm()
        response = await llm.complete([
            {"role": "system", "content": PLAYBOOK_SYSTEM},
            {"role": "user", "content": grounding},
        ])
        text = sanitize((response.content or "").strip())
        if not text:
            return False

        async with get_async_session() as session:
            row = await session.get(Watchlist, watchlist_id)
            if row is None:
                return False
            row.playbook = text
            row.playbook_updated_at = datetime.now(timezone.utc)
            await session.commit()
        return True
    except Exception:
        logger.exception("playbook generation failed for watchlist %d", watchlist_id)
        return False
