from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, model_validator
from sqlalchemy import func, select

from dealbot.agents.nl_watchlist import NLWatchlistAgent
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.api.routes.hunts import HuntListResponse, to_summary
from dealbot.db.models import Deal, Hunt, Listing, User, Watchlist, WatchlistRanking
from dealbot.lifecycle import stale_cutoff
from dealbot.recsys.intent import compose_intent_document
from dealbot.recsys.rank_cache import rankings_are_stale, recompute_rankings
from dealbot.db.semantic import retrieve_similar_deals
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.llm.ollama import OllamaClient
from dealbot.llm.vllm import vLLMClient
from dealbot.schemas import ChatMessage, TurnResult, WatchlistContext, WatchlistContextPatch

router = APIRouter(prefix="/watchlists", tags=["watchlists"])

WATCHLIST_TTL_DAYS = 60
FREE_WATCHLIST_CAP = 1
PRO_WATCHLIST_CAP = 5

# Shortlist handed to the listwise ranker. Large enough that dense retrieval
# has room to be wrong, small enough to rank in a handful of windowed calls.
CANDIDATE_POOL_SIZE = 150


def _get_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        # Scout runs on the judgment-tier model: Haiku reliably ignores the
        # close-instead-of-fishing gate (probed 3x), Sonnet obeys it. A whole
        # onboarding conversation costs ~2-3¢ — the first impression is worth it.
        return BedrockClient(
            model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
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
    context: WatchlistContext | None = None
    min_score: int = 50

    @model_validator(mode="after")
    def require_description_or_context(self) -> "WatchlistCreate":
        if not self.description and not self.context:
            raise ValueError("Provide either a description or a context.")
        return self


class WatchlistResponse(BaseModel):
    id: int
    name: str
    min_score: int
    expires_at: Optional[str]
    context: Optional[dict] = None
    playbook: Optional[str] = None
    playbook_updated_at: Optional[str] = None
    # Sweep state for the card's top-right pill (list endpoint populates).
    running_hunt_id: Optional[int] = None
    last_hunt_at: Optional[str] = None
    next_hunt_at: Optional[str] = None


class WatchlistDealResponse(BaseModel):
    id: int
    title: str
    source: str
    url: Optional[str]
    listed_price: float
    sale_price: float
    deal_score: Optional[int]
    category: str
    real_discount_pct: Optional[float]
    student_eligible: bool
    condition: str

    model_config = {"from_attributes": True}


class WatchlistDealsResponse(BaseModel):
    deals: list[WatchlistDealResponse]
    filtered: bool


@router.post("/chat", response_model=TurnResult)
async def chat_turn(
    body: ChatTurnRequest,
    current_user: User = Depends(get_current_user),
) -> TurnResult:
    """Single stateless conversation turn with Scout, the watchlist agent."""
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
    if not body.context or not body.context.product_query:
        raise HTTPException(status_code=400, detail="Context with product_query required.")

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
                f"Pro members can have up to {PRO_WATCHLIST_CAP} active agents. Delete one to deploy a new one."
                if current_user.is_pro
                else f"Free accounts are limited to {FREE_WATCHLIST_CAP} agent. Upgrade to pro for up to {PRO_WATCHLIST_CAP}."
            )
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=detail)

        # The user's preference vector: the whole elicited context, not just the
        # query string. See dealbot/recsys/intent.py.
        intent_embedding = await embed_text(compose_intent_document(body.context))

        watchlist = Watchlist(
            user_id=current_user.id,
            name=body.name,
            min_score=body.min_score,
            expires_at=_expiry(),
            context=body.context.model_dump_json(),
            intent_embedding=intent_embedding or None,
        )
        session.add(watchlist)
        await session.commit()
        await session.refresh(watchlist)
        wl_id = watchlist.id

    # Dispatch the research agent + Scout's playbook (background via Celery)
    try:
        from dealbot.worker.tasks import generate_playbook_task, research_for_agent
        # First hunt: highest priority — new users never wait behind cron.
        research_for_agent.apply_async(
            args=[wl_id], kwargs={"first_hunt": True}, priority=0,
        )
        generate_playbook_task.delay(wl_id)
    except Exception:
        pass  # worker not running in dev — fail silently

    return WatchlistResponse(
        id=wl_id,
        name=watchlist.name,
        min_score=watchlist.min_score,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
        context=json.loads(watchlist.context) if watchlist.context else None,
        playbook=watchlist.playbook,
        playbook_updated_at=(
            watchlist.playbook_updated_at.isoformat()
            if watchlist.playbook_updated_at else None
        ),
    )


@router.get("/{watchlist_id}/deals", response_model=WatchlistDealsResponse)
async def list_watchlist_deals(
    watchlist_id: int,
    limit: int = Query(20, ge=1, le=50),
    current_user: User = Depends(get_current_user),
) -> WatchlistDealsResponse:
    """Return deals matched to the watchlist's intent embedding, filtered by context."""
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")

        ctx = (
            WatchlistContext.model_validate_json(watchlist.context)
            if watchlist.context
            else None
        )

        # Cosine-match the intent embedding to all deals in the pool
        deals: list[Deal] = []
        if watchlist.intent_embedding is not None:
            deals = await retrieve_similar_deals(
                watchlist.intent_embedding, session, threshold=0.55, k=limit * 2,
            )

    # Filter out validation-rejected deals
    deals = [d for d in deals if d.legitimate]

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
                if any(b.lower() in d.title.lower() or b.lower() in d.source.lower()
                       for b in ctx.brands)
            ]
        if ctx.min_discount_pct:
            strict = [
                d for d in deals
                if d.real_discount_pct and d.real_discount_pct >= ctx.min_discount_pct
            ]
            if strict:
                deals = strict
            else:
                filtered = False

    # Keep cosine-similarity ordering (retrieve_similar_deals already sorted by relevance)
    return WatchlistDealsResponse(
        deals=[WatchlistDealResponse(
            id=d.id, title=d.title, source=d.source, url=d.url,
            listed_price=d.listed_price, sale_price=d.sale_price,
            deal_score=d.deal_score, category=d.category,
            real_discount_pct=d.real_discount_pct,
            student_eligible=d.student_eligible, condition=d.condition,
        ) for d in deals[:limit]],
        filtered=filtered,
    )


@router.post("/{watchlist_id}/renew", response_model=WatchlistResponse)
async def renew_watchlist(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    """Pro-only: reset expires_at to 60 days from now."""
    if not current_user.is_pro:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Agent renewal is a pro feature.")
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
        watchlist.expires_at = _expiry()
        await session.commit()
        await session.refresh(watchlist)
    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        min_score=watchlist.min_score,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
        playbook=watchlist.playbook,
        playbook_updated_at=(
            watchlist.playbook_updated_at.isoformat()
            if watchlist.playbook_updated_at else None
        ),
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
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")

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
        if body.buyer_profile is not None:
            ctx.buyer_profile = body.buyer_profile
        if body.quality_bar is not None:
            ctx.quality_bar = body.quality_bar
        if body.appearance_notes is not None:
            ctx.appearance_notes = body.appearance_notes
        if body.attributes is not None:
            ctx.attributes = body.attributes

        watchlist.context = ctx.model_dump_json()
        # Any context edit changes what the user wants; a vector describing the
        # pre-edit intent would silently mis-retrieve until the next edit.
        # embed_text returns [] on backend failure — keeping a slightly stale
        # vector beats overwriting a good one with nothing.
        revised = await embed_text(compose_intent_document(ctx))
        if revised:
            watchlist.intent_embedding = revised
        await session.commit()
        await session.refresh(watchlist)

    # The vector moved — re-rank and refresh the playbook in the background.
    # Broker-down is tolerable: the read path's staleness backstop will catch
    # the rankings later, and the playbook just stays on its previous text.
    try:
        from dealbot.worker.tasks import generate_playbook_task, recompute_rankings_task
        recompute_rankings_task.delay(watchlist_id)
        generate_playbook_task.delay(watchlist_id)
    except Exception:
        pass

    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        min_score=watchlist.min_score,
        expires_at=watchlist.expires_at.isoformat() if watchlist.expires_at else None,
        context=json.loads(watchlist.context) if watchlist.context else None,
        playbook=watchlist.playbook,
        playbook_updated_at=(
            watchlist.playbook_updated_at.isoformat()
            if watchlist.playbook_updated_at else None
        ),
    )


# ---------------------------------------------------------------------------
# v14 hunt + reranked listings endpoints
# ---------------------------------------------------------------------------

class HuntTriggerResponse(BaseModel):
    watchlist_id: int
    dispatched: bool
    detail: str


class ListingResponse(BaseModel):
    id: int
    marketplace: str
    title: str
    price: float
    currency: str
    url: str                                    # raw (clickable) URL
    image_url: Optional[str]
    location: Optional[str]
    condition: str
    relevance_score: float
    reason: Optional[str] = None                # one-line "why this listing"
    headline: Optional[str] = None              # Scout's cached read, one line (✦)
    flags: Optional[dict] = None                # objective inspection flags (trust spec)
    repost_suspect: bool = False
    first_seen_at: str
    last_seen_at: str


class ListingsResponse(BaseModel):
    listings: list[ListingResponse]
    total_candidates: int                       # rankings in the cache
    reranked: bool                              # False if ranking degraded
    computed_at: Optional[str] = None           # when the cache was built


@router.post(
    "/{watchlist_id}/hunt", response_model=HuntTriggerResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_hunt(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> HuntTriggerResponse:
    """Fire a fresh v14 hunt for this watchlist. Enqueues a Celery task and
    returns immediately; poll GET /{id}/listings to see results.

    Metering: manual hunts share the tier cadence budget with scheduled ones
    (free daily, pro hourly) — each hunt is real browser + LLM spend. Inside
    the window → 429; a dispatch stamps last_hunt_at so the scheduler can't
    double-spend the same window."""
    from dealbot.worker.scheduler import cadence_minutes

    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.",
            )
        if not watchlist.context:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Watchlist has no context; cannot hunt.",
            )

        if watchlist.last_hunt_at is not None:
            last = watchlist.last_hunt_at
            if last.tzinfo is None:  # sqlite returns stored UTC naive
                last = last.replace(tzinfo=timezone.utc)
            cadence = timedelta(minutes=cadence_minutes(watchlist, current_user))
            next_at = last + cadence
            now = datetime.now(timezone.utc)
            if now < next_at:
                wait_min = max(1, int((next_at - now).total_seconds() // 60))
                tier_hint = "" if current_user.is_pro else " Pro agents hunt hourly."
                raise HTTPException(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    detail=f"This agent hunted recently — next hunt available in ~{wait_min} min.{tier_hint}",
                )

        watchlist.last_hunt_at = datetime.now(timezone.utc)
        await session.commit()

    try:
        from dealbot.worker.tasks import research_for_agent
        research_for_agent.delay(watchlist_id)
        return HuntTriggerResponse(
            watchlist_id=watchlist_id, dispatched=True,
            detail="Hunt dispatched to Celery worker.",
        )
    except Exception as exc:
        # Worker not running (dev). Report but don't 500 — client polls listings.
        return HuntTriggerResponse(
            watchlist_id=watchlist_id, dispatched=False,
            detail=f"Dispatch failed ({type(exc).__name__}); worker offline?",
        )


@router.get("/{watchlist_id}/listings", response_model=ListingsResponse)
async def list_watchlist_listings(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> ListingsResponse:
    """Serve the precomputed ranking cache (~50ms; no LLM on the request path).

    Recomputes are event-driven — hunt completion, context edits — with a lazy
    staleness backstop here: rankings older than RANKINGS_STALE_HOURS are
    served as-is while a background recompute is enqueued (stale-while-
    revalidate). Only a watchlist that has never been ranked computes inline,
    paying the old live latency exactly once.

    Returns the FULL ordered ranking: with compute off the request path,
    truncating to a top-N buys nothing — the UI shows the quality continuum.
    """
    async def _read() -> list[tuple[WatchlistRanking, Listing]]:
        async with get_async_session() as session:
            watchlist = await session.get(Watchlist, watchlist_id)
            if watchlist is None or watchlist.user_id != current_user.id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.",
                )
            # The cache is filtered at recompute time, but listings age and
            # sell BETWEEN recomputes: the read re-checks liveness so a gone
            # listing never lingers in someone's top picks.
            return list((await session.execute(
                select(WatchlistRanking, Listing)
                .join(Listing, Listing.id == WatchlistRanking.listing_id)
                .where(WatchlistRanking.watchlist_id == watchlist_id)
                .where(Listing.last_seen_at >= stale_cutoff())
                .where(Listing.sold_at.is_(None))
                .order_by(WatchlistRanking.position)
            )).all())

    rows = await _read()
    if not rows:
        # Never ranked (or cache cleared): compute inline, once.
        await recompute_rankings(watchlist_id)
        rows = await _read()
    elif rankings_are_stale(rows[0][0].computed_at):
        # Serve stale immediately; refresh in the background.
        try:
            from dealbot.worker.tasks import recompute_rankings_task
            recompute_rankings_task.delay(watchlist_id)
        except Exception:
            pass  # broker down — stale keeps serving; next read retries

    # ✦ teasers + trust flags: cached Tier A output for whatever's inspected.
    headlines: dict[int, str] = {}
    flags_by_id: dict[int, dict] = {}
    if rows:
        from dealbot.db.models import ListingInspection

        async with get_async_session() as session:
            cached = (await session.execute(
                select(ListingInspection)
                .where(ListingInspection.listing_id.in_([l.id for _r, l in rows]))
                .where(ListingInspection.status == "ok")
            )).scalars().all()
        for row in cached:
            if row.flags:
                flags_by_id[row.listing_id] = row.flags
            try:
                headline = json.loads(row.report or "{}").get("headline")
                if headline:
                    headlines[row.listing_id] = headline
            except (json.JSONDecodeError, AttributeError):
                continue

    return ListingsResponse(
        listings=[
            ListingResponse(
                id=listing.id, marketplace=listing.marketplace,
                title=listing.title, price=listing.price,
                currency=listing.currency, url=listing.raw_url,
                image_url=listing.image_url, location=listing.location,
                condition=listing.condition,
                relevance_score=ranking.score, reason=ranking.reason,
                headline=headlines.get(listing.id),
                flags=flags_by_id.get(listing.id),
                repost_suspect=listing.repost_suspect,
                first_seen_at=listing.first_seen_at.isoformat(),
                last_seen_at=listing.last_seen_at.isoformat(),
            )
            for ranking, listing in rows
        ],
        total_candidates=len(rows),
        reranked=any(r.score > 0 for r, _l in rows),
        computed_at=rows[0][0].computed_at.isoformat() if rows else None,
    )


@router.delete("/{watchlist_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_watchlist(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> None:
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
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

        # Sweep pill state: one query for all cards' running hunts.
        from dealbot.api.routes.hunts import _next_hunt_at
        from dealbot.db.models import Hunt

        running: dict[int, int] = {}
        if watchlists:
            hunt_rows = (await session.execute(
                select(Hunt.watchlist_id, Hunt.id)
                .where(Hunt.watchlist_id.in_([w.id for w in watchlists]))
                .where(Hunt.status == "running")
            )).all()
            running = {wl_id: hunt_id for wl_id, hunt_id in hunt_rows}

        responses = []
        for wl in watchlists:
            wl.expires_at = _expiry()
            next_at = _next_hunt_at(wl, current_user.is_pro)
            responses.append(WatchlistResponse(
                id=wl.id,
                name=wl.name,
                min_score=wl.min_score,
                expires_at=wl.expires_at.isoformat() if wl.expires_at else None,
                context=json.loads(wl.context) if wl.context else None,
                playbook=wl.playbook,
                playbook_updated_at=(
                    wl.playbook_updated_at.isoformat()
                    if wl.playbook_updated_at else None
                ),
                running_hunt_id=running.get(wl.id),
                last_hunt_at=wl.last_hunt_at.isoformat() if wl.last_hunt_at else None,
                next_hunt_at=next_at.isoformat() if next_at else None,
            ))

        await session.commit()

    return responses


@router.get("/{watchlist_id}/hunts", response_model=HuntListResponse)
async def list_watchlist_hunts(
    watchlist_id: int,
    limit: int = Query(10, ge=1, le=100),
    current_user: User = Depends(get_current_user),
) -> HuntListResponse:
    """This agent's hunt history, newest first."""
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.",
            )
        rows = (
            await session.execute(
                select(Hunt)
                .where(Hunt.watchlist_id == watchlist_id)
                .order_by(Hunt.started_at.desc())
                .limit(limit)
            )
        ).scalars().all()
    return HuntListResponse(hunts=[to_summary(h, watchlist.name) for h in rows])
