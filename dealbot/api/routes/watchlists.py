from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select

from dealbot.agents.keyword_extractor import extract_keywords
from dealbot.agents.nl_watchlist import NLWatchlistAgent
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal, User, Watchlist, WatchlistKeyword
from dealbot.db.rag import retrieve_similar_deals
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.vllm import vLLMClient
from dealbot.schemas import ChatMessage, TurnResult, WatchlistContext, WatchlistContextPatch

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

WATCHLIST_TTL_DAYS = 60  # inactive watchlists expire after 60 days
FREE_WATCHLIST_CAP = 1
PRO_WATCHLIST_CAP = 5


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "openai":
        from dealbot.llm.openai_client import OpenAIClient
        return OpenAIClient()
    if backend == "groq":
        from dealbot.llm.groq_client import GroqClient
        return GroqClient()
    if backend == "vllm":
        return vLLMClient()
    return OllamaClient()


class ChatTurnRequest(BaseModel):
    messages: list[ChatMessage]
    context: WatchlistContext | None = None


def _expiry() -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=WATCHLIST_TTL_DAYS)


class WatchlistCreate(BaseModel):
    name: str
    description: str | None = None
    keywords: list[str] = []
    context: WatchlistContext | None = None  # from NL chat flow
    min_score: int = 50
    alert_tier_threshold: str = "digest"

    @model_validator(mode="after")
    def require_description_or_keywords(self) -> "WatchlistCreate":
        if not self.description and not self.keywords and not self.context:
            raise ValueError("Provide either a description, keywords, or a context.")
        return self


class WatchlistResponse(BaseModel):
    id: int
    name: str
    keywords: list[str]
    min_score: int
    alert_tier_threshold: str
    expires_at: Optional[str]
    context: Optional[dict] = None


class WatchlistDealResponse(BaseModel):
    id: int
    title: str
    source: str
    url: Optional[str]
    listed_price: float
    sale_price: float
    score: int
    alert_tier: str
    category: str
    real_discount_pct: Optional[float]
    student_eligible: bool
    condition: str

    model_config = {"from_attributes": True}


class WatchlistDealsResponse(BaseModel):
    deals: list[WatchlistDealResponse]
    filtered: bool  # False = min_discount_pct was relaxed (fallback)


@router.post("/chat", response_model=TurnResult)
async def chat_turn(
    body: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
) -> TurnResult:
    """Single stateless conversation turn with Dexter, the watchlist agent."""
    agent = NLWatchlistAgent(_get_llm())
    return await agent.turn(
        messages=[m.model_dump() for m in body.messages],
        context=body.context,
    )


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    body: WatchlistCreate,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    async with get_async_session() as session:
        now = datetime.now(timezone.utc)
        count_result = await session.execute(
            select(func.count(Watchlist.id)).where(
                Watchlist.user_id == current_user.id,
                (Watchlist.expires_at == None) | (Watchlist.expires_at > now),  # noqa: E711
            )
        )
        cap = PRO_WATCHLIST_CAP if current_user.is_pro else FREE_WATCHLIST_CAP
        if count_result.scalar_one() >= cap:
            detail = (
                f"Pro members can have up to {PRO_WATCHLIST_CAP} active watchlists. Delete one to create a new one."
                if current_user.is_pro
                else f"Free accounts are limited to {FREE_WATCHLIST_CAP} watchlist. Upgrade to pro for up to {PRO_WATCHLIST_CAP}."
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

        if body.context and body.context.keywords:
            keywords = [kw.lower().strip() for kw in body.context.keywords]
        elif body.description and not body.keywords:
            keywords = await extract_keywords(body.description, _get_llm())
        else:
            keywords = [kw.lower().strip() for kw in body.keywords]

        watchlist = Watchlist(
            user_id=current_user.id,
            name=body.name,
            min_score=body.min_score,
            alert_tier_threshold=body.alert_tier_threshold,
            expires_at=_expiry(),
            context=body.context.model_dump_json() if body.context else None,
        )
        session.add(watchlist)
        await session.flush()

        for kw in keywords:
            embedding = await embed_text(kw)
            session.add(WatchlistKeyword(
                watchlist_id=watchlist.id,
                keyword=kw,
                embedding=embedding or None,
            ))

        await session.commit()
        await session.refresh(watchlist)

    # Dispatch immediate hunt for each keyword (runs in background via Celery)
    try:
        from dealbot.worker.tasks import hunt_keyword
        for kw in keywords:
            hunt_keyword.delay(kw)
    except Exception:
        pass  # worker not running in dev — fail silently

    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        keywords=keywords,
        min_score=watchlist.min_score,
        alert_tier_threshold=watchlist.alert_tier_threshold,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
        context=json.loads(watchlist.context) if watchlist.context else None,
    )


@router.get("/{watchlist_id}/deals", response_model=WatchlistDealsResponse)
async def list_watchlist_deals(
    watchlist_id: int,
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
) -> WatchlistDealsResponse:
    """Return deals matched to watchlist keywords, filtered by WatchlistContext."""
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found.")

        ctx = (
            WatchlistContext.model_validate_json(watchlist.context)
            if watchlist.context
            else None
        )

        kw_result = await session.execute(
            select(WatchlistKeyword).where(WatchlistKeyword.watchlist_id == watchlist_id)
        )
        keywords = kw_result.scalars().all()

        seen_ids: set[int] = set()
        deals: list[Deal] = []
        for kw in keywords:
            if kw.embedding is None:
                continue
            similar = await retrieve_similar_deals(kw.embedding, session, k=10)
            for d in similar:
                if d.id not in seen_ids:
                    seen_ids.add(d.id)
                    deals.append(d)

    # Apply context filters
    filtered = True
    if ctx:
        if ctx.max_budget:
            deals = [d for d in deals if d.sale_price <= ctx.max_budget]
        if ctx.condition:
            deals = [d for d in deals if d.condition in ctx.condition]
        if ctx.brands:
            deals = [
                d for d in deals
                if any(
                    b.lower() in d.title.lower() or b.lower() in d.source.lower()
                    for b in ctx.brands
                )
            ]
        if ctx.min_discount_pct:
            strict = [
                d for d in deals
                if d.real_discount_pct and d.real_discount_pct >= ctx.min_discount_pct
            ]
            if strict:
                deals = strict
            else:
                filtered = False  # fallback: relax min_discount_pct

    deals.sort(key=lambda d: d.score, reverse=True)
    return WatchlistDealsResponse(
        deals=[
            WatchlistDealResponse(
                id=d.id,
                title=d.title,
                source=d.source,
                url=d.url,
                listed_price=d.listed_price,
                sale_price=d.sale_price,
                score=d.score,
                alert_tier=d.alert_tier,
                category=d.category,
                real_discount_pct=d.real_discount_pct,
                student_eligible=d.student_eligible,
                condition=d.condition,
            )
            for d in deals[:limit]
        ],
        filtered=filtered,
    )


@router.post("/{watchlist_id}/renew", response_model=WatchlistResponse)
async def renew_watchlist(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    """Pro-only: reset expires_at to 60 days from now."""
    if not current_user.is_pro:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Watchlist renewal is a pro feature.")
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found.")
        watchlist.expires_at = _expiry()
        kw_result = await session.execute(
            select(WatchlistKeyword).where(WatchlistKeyword.watchlist_id == watchlist.id)
        )
        keywords = [k.keyword for k in kw_result.scalars().all()]
        await session.commit()
        await session.refresh(watchlist)
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        keywords=keywords,
        min_score=watchlist.min_score,
        alert_tier_threshold=watchlist.alert_tier_threshold,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
    )


@router.patch("/{watchlist_id}", response_model=WatchlistResponse)
async def patch_watchlist(
    watchlist_id: int,
    body: WatchlistContextPatch,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    """Update editable context fields (budget, discount, condition, brands)."""
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found.")

        ctx = (
            WatchlistContext.model_validate_json(watchlist.context)
            if watchlist.context
            else WatchlistContext(product_query="", keywords=[])
        )

        if body.max_budget is not None:
            ctx.max_budget = body.max_budget
        if body.min_discount_pct is not None:
            ctx.min_discount_pct = body.min_discount_pct
        if body.condition is not None:
            ctx.condition = body.condition
        if body.brands is not None:
            ctx.brands = body.brands

        watchlist.context = ctx.model_dump_json()
        await session.commit()
        await session.refresh(watchlist)

        kw_result = await session.execute(
            select(WatchlistKeyword).where(WatchlistKeyword.watchlist_id == watchlist.id)
        )
        keywords = [k.keyword for k in kw_result.scalars().all()]

    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        keywords=keywords,
        min_score=watchlist.min_score,
        alert_tier_threshold=watchlist.alert_tier_threshold,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
        context=json.loads(watchlist.context) if watchlist.context else None,
    )


@router.delete("/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> None:
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Watchlist not found.")
        await session.delete(watchlist)
        await session.commit()


@router.get("", response_model=list[WatchlistResponse])
async def list_watchlists(
    current_user: User = Depends(get_current_user),
) -> list[WatchlistResponse]:
    now = datetime.now(timezone.utc)
    async with get_async_session() as session:
        result = await session.execute(
            select(Watchlist).where(
                Watchlist.user_id == current_user.id,
                (Watchlist.expires_at == None) | (Watchlist.expires_at > now),  # noqa: E711
            )
        )
        watchlists = result.scalars().all()

        responses = []
        for wl in watchlists:
            # Refresh expiry on activity — keeps active users' watchlists alive
            wl.expires_at = _expiry()

            kw_result = await session.execute(
                select(WatchlistKeyword).where(WatchlistKeyword.watchlist_id == wl.id)
            )
            keywords = [k.keyword for k in kw_result.scalars().all()]
            responses.append(WatchlistResponse(
                id=wl.id,
                name=wl.name,
                keywords=keywords,
                min_score=wl.min_score,
                alert_tier_threshold=wl.alert_tier_threshold,
                expires_at=wl.expires_at.isoformat() if wl.expires_at else None,
            ))

        await session.commit()

    return responses
