"""Agent card data surfaces: market analysis + ask-Scout-about-this-market.

GET  /watchlists/{id}/market  deterministic stats over the agent's comp set
POST /watchlists/{id}/ask     stateless Scout Q&A grounded in the playbook,
                              the market numbers, and the buyer profile
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from dealbot.agents.playbook import sanitize
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User, Watchlist, WatchlistRanking
from dealbot.llm.base import LLMClient
from dealbot.recsys.market_stats import agent_comps, compute_market
from dealbot.schemas import ChatMessage, WatchlistContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watchlists", tags=["market"])

_CHAT_HISTORY_MAX = 30
_TOP_PICKS = 5


def _chat_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


async def _owned_watchlist(watchlist_id: int, user: User) -> Watchlist:
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
    if watchlist is None or watchlist.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
    return watchlist


async def _pick_ids(watchlist_id: int) -> list[int]:
    from sqlalchemy import select

    async with get_async_session() as session:
        rows = (await session.execute(
            select(WatchlistRanking.listing_id)
            .where(WatchlistRanking.watchlist_id == watchlist_id)
            .order_by(WatchlistRanking.position)
            .limit(_TOP_PICKS)
        )).scalars().all()
    return list(rows)


class MarketResponse(BaseModel):
    n_live: int
    typical: int | None
    band: dict | None
    within_budget: int | None
    ceiling: float | None
    newest_find_hours: float | None
    histogram: list[dict]
    pick_prices: list[dict]
    structure: dict
    heat: dict
    negotiation: dict | None
    going_rate_prose: str | None    # the playbook's market section, if parsed


def _going_rate_section(playbook: str | None) -> str | None:
    """Extract 'The going rate' prose from the playbook's fixed headings."""
    if not playbook:
        return None
    try:
        start = playbook.index("The going rate") + len("The going rate")
        rest = playbook[start:]
        end = rest.index("How to haggle") if "How to haggle" in rest else len(rest)
        text = rest[:end].strip()
        return text or None
    except ValueError:
        return None


@router.get("/{watchlist_id}/market", response_model=MarketResponse)
async def market_analysis(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> MarketResponse:
    watchlist = await _owned_watchlist(watchlist_id, current_user)
    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    comps = await agent_comps(watchlist)
    picks = await _pick_ids(watchlist_id)
    market = compute_market(comps, context, pick_ids=picks)
    market["going_rate_prose"] = _going_rate_section(watchlist.playbook)
    return MarketResponse(**market)


ASK_SYSTEM = """You are Scout, the user's expert friend on this specific secondhand
market. You wrote the playbook below and computed the market numbers below; the
user is asking you questions about them.

Rules:
- Ground every claim in the playbook, the market numbers, or the buyer context
  provided. If something isn't knowable from those, say so plainly.
- You cannot take actions from this chat (no new sweeps, no contacting anyone).
  If asked, say what you'd do instead and point them at the right button.
- Short, direct, warm. Plain language. Never use em dashes. No stock closers."""


class AskRequest(BaseModel):
    messages: list[ChatMessage]


class AskResponse(BaseModel):
    reply: str


@router.post("/{watchlist_id}/ask", response_model=AskResponse)
async def ask_scout(
    watchlist_id: int,
    body: AskRequest,
    current_user: User = Depends(get_current_user),
) -> AskResponse:
    watchlist = await _owned_watchlist(watchlist_id, current_user)
    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    comps = await agent_comps(watchlist)
    market = compute_market(comps, context)

    grounding_parts = [f"Category: {context.product_query}" if context else "Category: unknown"]
    if context and context.max_budget:
        grounding_parts.append(f"Buyer budget: ${context.max_budget:.0f}")
    if context and context.buyer_profile:
        grounding_parts.append(f"Buyer profile: {context.buyer_profile}")
    grounding_parts.append("Market numbers (yours, computed): " + json.dumps({
        k: market[k] for k in ("n_live", "typical", "band", "within_budget",
                               "newest_find_hours", "heat", "negotiation", "structure")
    }))
    if watchlist.playbook:
        grounding_parts.append(f"Your playbook:\n{watchlist.playbook}")

    history = [
        {"role": m.role, "content": m.content}
        for m in body.messages[-_CHAT_HISTORY_MAX:]
        if m.role in ("user", "assistant")
    ]
    if not history:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty chat.")

    messages = [
        {"role": "system", "content": ASK_SYSTEM},
        {"role": "system", "content": "\n\n".join(grounding_parts)},
        *history,
    ]
    try:
        response = await _chat_llm().complete(messages)
        reply = sanitize((response.content or "").strip())
    except Exception:
        logger.exception("watchlist ask failed for %d", watchlist_id)
        reply = ""
    if not reply:
        reply = "I hit a snag answering that one. Give it another try in a moment."
    return AskResponse(reply=reply)
