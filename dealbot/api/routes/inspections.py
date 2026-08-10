"""Deal Inspector API: send a listing to Scout, read the report, chat about it.

POST /listings/{id}/inspect     run (or read cached) Tier A inspection
GET  /listings/{id}/inspection  cached report or 404
POST /listings/{id}/chat        stateless follow-up turn over the report

The chat is grounded, not agentic: Scout answers from what the inspection
already saw (report + extracted detail + playbook/profile when a watchlist
is given). Requests for new work (revisit, contact the seller) get an honest
text answer, never a browser session.
"""

from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.agents.playbook import sanitize
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import (
    InspectionMessage,
    InspectionWatch,
    Listing,
    ListingInspection,
    User,
    Watchlist,
)
from dealbot.llm.base import LLMClient
from dealbot.schemas import ChatMessage, WatchlistContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/listings", tags=["inspections"])

_CHAT_HISTORY_MAX = 30
FREE_INSPECTIONS_PER_MONTH = 10


def _month_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


async def _check_allowance(user_id: int) -> bool:
    """True if this user may run a FRESH inspection this month. Lazy monthly
    reset; cached reads never reach this. Pro is uncapped."""
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            return False
        if user.is_pro:
            return True
        month = _month_now()
        if user.inspections_month != month:
            user.inspections_month = month
            user.inspections_used = 0
            await session.commit()
        return user.inspections_used < FREE_INSPECTIONS_PER_MONTH


async def _count_inspection(user_id: int) -> None:
    async with get_async_session() as session:
        user = await session.get(User, user_id)
        if user is not None and not user.is_pro:
            user.inspections_used += 1
            await session.commit()


async def _watchlist_market(watchlist: Watchlist, context: WatchlistContext | None) -> dict | None:
    """Agent-scoped market numbers so listing chats and verdicts can place
    THIS deal against the playbook's rates. Best-effort."""
    try:
        from dealbot.recsys.market_stats import agent_comps, compute_market

        comps = await agent_comps(watchlist)
        return compute_market(comps, context)
    except Exception:
        logger.warning("market grounding failed for wl %d", watchlist.id, exc_info=True)
        return None


async def _record_watch(user_id: int, listing: Listing) -> None:
    """Inspecting = interest: start (or keep) a price-drop watch. Best-effort."""
    try:
        async with get_async_session() as session:
            existing = await session.get(InspectionWatch, (user_id, listing.id))
            if existing is None:
                session.add(InspectionWatch(
                    user_id=user_id, listing_id=listing.id,
                    price_at_inspection=listing.price,
                ))
                await session.commit()
    except Exception:
        logger.warning("inspection watch record failed (user %d, listing %d)",
                       user_id, listing.id, exc_info=True)


def _chat_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


class InspectionResponse(BaseModel):
    status: str                      # ok | listing_gone | error
    report: dict | None = None
    detail: dict | None = None
    comps: list[dict] = []
    cached: bool = False
    created_at: str | None = None


class InspectionChatRequest(BaseModel):
    messages: list[ChatMessage]
    watchlist_id: int | None = None


class InspectionChatResponse(BaseModel):
    reply: str


@router.post("/{listing_id}/inspect", response_model=InspectionResponse)
async def inspect_listing(
    listing_id: int,
    force: bool = Query(False),
    current_user: User = Depends(get_current_user),
) -> InspectionResponse:
    from dealbot.agents.inspector import get_cached_inspection, get_or_create_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")

    fresh_needed = force or (await get_cached_inspection(listing_id)) is None
    if fresh_needed and not await _check_allowance(current_user.id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                "Scout has used up this month's free looks. Pro removes the "
                "cap and inspects your top matches automatically."
            ),
        )

    result = await get_or_create_inspection(listing_id, force=force)
    if fresh_needed and result["status"] in ("ok", "listing_gone"):
        # A real look happened: count it against the allowance.
        await _count_inspection(current_user.id)
    if result["status"] == "ok":
        await _record_watch(current_user.id, listing)
    return InspectionResponse(**result)


@router.get("/{listing_id}/inspection", response_model=InspectionResponse)
async def read_inspection(
    listing_id: int,
    current_user: User = Depends(get_current_user),
) -> InspectionResponse:
    from dealbot.agents.inspector import get_cached_inspection

    cached = await get_cached_inspection(listing_id)
    if cached is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Not inspected yet.")
    return InspectionResponse(**cached)


VERDICT_SYSTEM = """You are Scout, the user's expert friend. You already inspected
this listing (your notes below). Now give YOUR TAKE FOR THIS SPECIFIC BUYER,
using their profile, budget, and your category playbook.

Cover, in flowing prose (no headings, no lists):
- Fit: does this listing suit what they actually need? Be honest if not.
- The number: what they should offer, and their walk-away, using ONLY the
  prices provided (asking price, market band, their budget).
- The opener: one short ready-to-send message to the seller making that offer,
  in quotes.

80-140 words. Second person, warm, direct. Plain text only: no markdown, no
asterisks, no bold, no headings. Never use em dashes. No greeting, no
sign-off."""


class VerdictRequest(BaseModel):
    watchlist_id: int


class VerdictResponse(BaseModel):
    verdict: str


@router.post("/{listing_id}/verdict", response_model=VerdictResponse)
async def inspection_verdict(
    listing_id: int,
    body: VerdictRequest,
    current_user: User = Depends(get_current_user),
) -> VerdictResponse:
    """Tier B: the personal read. Cheap text call over cached Tier A + the
    watchlist playbook + profile; computed on demand, never cached."""
    from dealbot.agents.inspector import get_cached_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
        watchlist = await session.get(Watchlist, body.watchlist_id)
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")
    if watchlist is None or watchlist.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")

    inspection = await get_cached_inspection(listing_id)
    if inspection is None or inspection["status"] != "ok":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scout needs a successful look before giving a personal take.",
        )

    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    market = await _watchlist_market(watchlist, context)
    grounding = _grounding(listing, inspection, watchlist.playbook, context, market)
    llm = _chat_llm()
    try:
        response = await llm.complete([
            {"role": "system", "content": VERDICT_SYSTEM},
            {"role": "user", "content": grounding},
        ])
        verdict = sanitize((response.content or "").strip())
    except Exception:
        logger.exception("verdict failed for listing %d", listing_id)
        verdict = ""
    if not verdict:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Scout could not put a take together just now. Try again shortly.",
        )
    return VerdictResponse(verdict=verdict)


CHAT_SYSTEM = """You are Scout, the user's expert friend who already took a close
look at ONE marketplace listing for them. The inspection notes below are what you
saw. The user is now chatting with you about it.

Rules:
- Ground every claim in the inspection notes, the listing facts, or the market
  numbers provided. If they ask something the inspection cannot answer, say so
  plainly and either point at the seller question that would settle it or offer
  a fresh look ("want me to take another look?").
- You cannot take new actions from this chat (no revisiting the page, no
  messaging the seller). Be upfront when asked; hand them exactly what to send
  instead.
- If buyer context is provided, tailor advice to it; otherwise keep advice
  general and say numbers depend on their budget.
- Short, direct, warm. Plain language. Never use em dashes. No stock closers."""


def _grounding(listing: Listing, inspection: dict, playbook: str | None,
               context: WatchlistContext | None,
               market: dict | None = None) -> str:
    parts = [
        f"Listing: {listing.title}",
        f"Price: ${listing.price:.2f} {listing.currency} on {listing.marketplace} · URL: {listing.raw_url}",
        f"Inspection status: {inspection['status']}",
    ]
    if inspection.get("report"):
        parts.append("Inspection notes (your own, from when you looked):")
        parts.append(json.dumps(inspection["report"], indent=1))
    if inspection.get("detail"):
        parts.append(f"Extracted page detail: {json.dumps(inspection['detail'])}")
    if context is not None:
        buyer = [f"Buyer's category: {context.product_query}"]
        if context.max_budget:
            buyer.append(f"budget ${context.max_budget:.0f}")
        if context.buyer_profile:
            buyer.append(f"profile: {context.buyer_profile}")
        parts.append("Buyer context: " + " · ".join(buyer))
    if market:
        parts.append(
            "Your market numbers for this category (computed; relate THIS "
            "listing's price to them when relevant): " + json.dumps({
                k: market.get(k) for k in ("typical", "band", "negotiation", "heat")
            })
        )
    if playbook:
        parts.append(f"Your playbook for this category:\n{playbook}")
    return "\n\n".join(parts)


@router.post("/{listing_id}/chat", response_model=InspectionChatResponse)
async def inspection_chat(
    listing_id: int,
    body: InspectionChatRequest,
    current_user: User = Depends(get_current_user),
) -> InspectionChatResponse:
    from dealbot.agents.inspector import get_cached_inspection

    async with get_async_session() as session:
        listing = await session.get(Listing, listing_id)
        watchlist = None
        if body.watchlist_id is not None:
            watchlist = await session.get(Watchlist, body.watchlist_id)
            if watchlist is None or watchlist.user_id != current_user.id:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
    if listing is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Listing not found.")

    inspection = await get_cached_inspection(listing_id)
    if inspection is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Scout has not looked at this listing yet. Inspect it first.",
        )

    context = None
    playbook = None
    market = None
    if watchlist is not None:
        playbook = watchlist.playbook
        if watchlist.context:
            context = WatchlistContext.model_validate_json(watchlist.context)
        market = await _watchlist_market(watchlist, context)

    history = [
        {"role": m.role, "content": m.content}
        for m in body.messages[-_CHAT_HISTORY_MAX:]
        if m.role in ("user", "assistant")
    ]
    if not history:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty chat.")

    messages = [
        {"role": "system", "content": CHAT_SYSTEM},
        {"role": "system", "content": _grounding(listing, inspection, playbook, context, market)},
        *history,
    ]
    llm = _chat_llm()
    failed = False
    try:
        response = await llm.complete(messages)
        reply = sanitize((response.content or "").strip())
    except Exception:
        logger.exception("inspection chat failed for listing %d", listing_id)
        reply = ""
    if not reply:
        failed = True
        reply = "I hit a snag answering that one. Give it another try in a moment."

    # A friend remembers the conversation: persist the exchange (but never a
    # snag apology — retries should not litter the thread). Best-effort.
    if not failed and history and history[-1]["role"] == "user":
        try:
            async with get_async_session() as session:
                session.add(InspectionMessage(
                    user_id=current_user.id, listing_id=listing_id,
                    role="user", content=history[-1]["content"],
                ))
                session.add(InspectionMessage(
                    user_id=current_user.id, listing_id=listing_id,
                    role="assistant", content=reply,
                ))
                await session.commit()
        except Exception:
            logger.warning("inspection message persist failed (listing %d)",
                           listing_id, exc_info=True)
    return InspectionChatResponse(reply=reply)


class ThreadMessage(BaseModel):
    role: str
    content: str
    created_at: str


class ThreadResponse(BaseModel):
    messages: list[ThreadMessage]


@router.get("/{listing_id}/messages", response_model=ThreadResponse)
async def read_thread(
    listing_id: int,
    current_user: User = Depends(get_current_user),
) -> ThreadResponse:
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionMessage)
            .where(InspectionMessage.user_id == current_user.id)
            .where(InspectionMessage.listing_id == listing_id)
            .order_by(InspectionMessage.id)
        )).scalars().all()
    return ThreadResponse(messages=[
        ThreadMessage(role=m.role, content=m.content, created_at=m.created_at.isoformat())
        for m in rows
    ])


class InspectedItem(BaseModel):
    listing_id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None
    sold: bool
    price_dropped: bool
    price_at_inspection: float
    last_message: str | None
    inspected_at: str


class InspectedResponse(BaseModel):
    items: list[InspectedItem]


@router.get("/inspected", response_model=InspectedResponse)
async def inspected_listings(
    current_user: User = Depends(get_current_user),
) -> InspectedResponse:
    """Everything this user has sent to Scout, newest first: the revisit
    surface. Watches double as the send-to-Scout record."""
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionWatch, Listing)
            .join(Listing, Listing.id == InspectionWatch.listing_id)
            .where(InspectionWatch.user_id == current_user.id)
            .order_by(InspectionWatch.created_at.desc())
            .limit(100)
        )).all()

        items: list[InspectedItem] = []
        for watch, listing in rows:
            last = (await session.execute(
                select(InspectionMessage.content)
                .where(InspectionMessage.user_id == current_user.id)
                .where(InspectionMessage.listing_id == listing.id)
                .order_by(InspectionMessage.id.desc())
                .limit(1)
            )).scalar_one_or_none()
            items.append(InspectedItem(
                listing_id=listing.id,
                title=listing.title,
                price=listing.price,
                currency=listing.currency,
                marketplace=listing.marketplace,
                url=listing.raw_url,
                image_url=listing.image_url,
                sold=listing.sold_at is not None,
                price_dropped=listing.price < watch.price_at_inspection,
                price_at_inspection=watch.price_at_inspection,
                last_message=(last[:140] if last else None),
                inspected_at=watch.created_at.isoformat(),
            ))
    return InspectedResponse(items=items)
