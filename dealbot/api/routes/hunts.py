"""Hunts listing routes — Mission Control renders DB state on load from here,
then augments live via the SSE stream."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntLane, User, Watchlist

router = APIRouter(prefix="/hunts", tags=["hunts"])


class HuntSummary(BaseModel):
    id: int
    watchlist_id: int
    watchlist_name: str
    status: str
    started_at: datetime
    finished_at: datetime | None
    offer_count: int
    persisted_count: int
    new_listing_count: int
    error: str | None
    next_hunt_at: datetime | None = None


class HuntListResponse(BaseModel):
    hunts: list[HuntSummary]


def to_summary(
    hunt: Hunt, watchlist_name: str, next_hunt_at: datetime | None = None,
) -> HuntSummary:
    return HuntSummary(
        id=hunt.id,
        watchlist_id=hunt.watchlist_id,
        watchlist_name=watchlist_name,
        status=hunt.status,
        started_at=hunt.started_at,
        finished_at=hunt.finished_at,
        offer_count=hunt.offer_count,
        persisted_count=hunt.persisted_count,
        new_listing_count=hunt.new_listing_count,
        error=hunt.error,
        next_hunt_at=next_hunt_at,
    )


def _next_hunt_at(watchlist: Watchlist, is_pro: bool) -> datetime | None:
    """When the scheduler will fire this watchlist next — the card's
    post-completion countdown. Mirrors the scheduler's cadence rule."""
    if not watchlist.hunting_enabled or watchlist.last_hunt_at is None:
        return None
    minutes = watchlist.hunt_frequency_minutes or (60 if is_pro else 1440)
    return watchlist.last_hunt_at + timedelta(minutes=minutes)


@router.get("", response_model=HuntListResponse)
async def list_hunts(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
) -> HuntListResponse:
    async with get_async_session() as session:
        stmt = (
            select(Hunt, Watchlist)
            .join(Watchlist, Hunt.watchlist_id == Watchlist.id)
            .where(Watchlist.user_id == current_user.id)
            .order_by(Hunt.started_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Hunt.status == status)
        rows = (await session.execute(stmt)).all()
    return HuntListResponse(hunts=[
        to_summary(h, wl.name, _next_hunt_at(wl, current_user.is_pro))
        for h, wl in rows
    ])


class HuntLaneResponse(BaseModel):
    query: str
    marketplace: str
    status: str
    pages: int
    done_reason: str | None
    frame: str | None = None          # latest viewport frame (data URL)


async def _lane_frames(hunt_id: int, lanes: list[HuntLane]) -> dict[tuple[str, str], str]:
    """Latest persisted frame per lane from Redis; {} on any failure."""
    import os

    import redis.asyncio as aioredis

    try:
        client = aioredis.from_url(os.environ.get("REDIS_URL", "redis://localhost:6379/0"))
        keys = [f"lane_frame:{hunt_id}:{l.marketplace}:{l.query}" for l in lanes]
        values = await client.mget(keys)
        await client.aclose()
        return {
            (l.marketplace, l.query): v.decode() if isinstance(v, bytes) else v
            for l, v in zip(lanes, values) if v
        }
    except Exception:
        return {}


@router.get("/{hunt_id}/lanes", response_model=list[HuntLaneResponse])
async def list_hunt_lanes(
    hunt_id: int,
    current_user: User = Depends(get_current_user),
) -> list[HuntLaneResponse]:
    """Persisted lane state — Mission Control seeds from this on load, then
    the SSE stream augments live. Survives refresh; screenshots do not
    (they re-arrive within a turn)."""
    async with get_async_session() as session:
        rows = (await session.execute(
            select(HuntLane)
            .join(Hunt, Hunt.id == HuntLane.hunt_id)
            .join(Watchlist, Watchlist.id == Hunt.watchlist_id)
            .where(HuntLane.hunt_id == hunt_id,
                   Watchlist.user_id == current_user.id)
            .order_by(HuntLane.marketplace, HuntLane.query)
        )).scalars().all()
    frames = await _lane_frames(hunt_id, list(rows))
    return [
        HuntLaneResponse(
            query=lane.query, marketplace=lane.marketplace,
            status=lane.status, pages=lane.pages, done_reason=lane.done_reason,
            frame=frames.get((lane.marketplace, lane.query)),
        )
        for lane in rows
    ]


class SweepListingResponse(BaseModel):
    id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None
    matched: bool                 # cleared the agent's bar (in rankings, non-weak)


class SweepListingsResponse(BaseModel):
    listings: list[SweepListingResponse]
    total: int


@router.get("/{hunt_id}/listings", response_model=SweepListingsResponse)
async def hunt_listings(
    hunt_id: int,
    current_user: User = Depends(get_current_user),
) -> SweepListingsResponse:
    """Everything the sweep surfaced (the card's deepest disclosure layer):
    every unique listing linked to this hunt, matched or not."""
    from dealbot.db.models import HuntListing, Listing, WatchlistRanking
    from dealbot.recsys.market_stats import WEAK_SCORE

    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hunt not found.")
        watchlist = await session.get(Watchlist, hunt.watchlist_id)
        if watchlist is None or watchlist.user_id != current_user.id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Hunt not found.")

        rows = (await session.execute(
            select(Listing)
            .join(HuntListing, HuntListing.listing_id == Listing.id)
            .where(HuntListing.hunt_id == hunt_id)
            .order_by(Listing.price)
        )).scalars().all()

        matched_ids = set((await session.execute(
            select(WatchlistRanking.listing_id)
            .where(WatchlistRanking.watchlist_id == hunt.watchlist_id)
            .where(WatchlistRanking.score >= WEAK_SCORE)
        )).scalars().all())

    return SweepListingsResponse(
        listings=[
            SweepListingResponse(
                id=l.id, title=l.title, price=l.price, currency=l.currency,
                marketplace=l.marketplace, url=l.raw_url, image_url=l.image_url,
                matched=l.id in matched_ids,
            )
            for l in rows
        ],
        total=len(rows),
    )
