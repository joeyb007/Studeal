"""Hunts listing routes — Mission Control renders DB state on load from here,
then augments live via the SSE stream."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
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
    return [
        HuntLaneResponse(
            query=lane.query, marketplace=lane.marketplace,
            status=lane.status, pages=lane.pages, done_reason=lane.done_reason,
        )
        for lane in rows
    ]
