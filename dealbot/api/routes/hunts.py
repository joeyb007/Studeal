"""Hunts listing routes — Mission Control renders DB state on load from here,
then augments live via the SSE stream."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, User, Watchlist

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


class HuntListResponse(BaseModel):
    hunts: list[HuntSummary]


def to_summary(hunt: Hunt, watchlist_name: str) -> HuntSummary:
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
    )


@router.get("", response_model=HuntListResponse)
async def list_hunts(
    status: str | None = Query(default=None),
    limit: int = Query(default=20, ge=1, le=100),
    current_user: User = Depends(get_current_user),
) -> HuntListResponse:
    async with get_async_session() as session:
        stmt = (
            select(Hunt, Watchlist.name)
            .join(Watchlist, Hunt.watchlist_id == Watchlist.id)
            .where(Watchlist.user_id == current_user.id)
            .order_by(Hunt.started_at.desc())
            .limit(limit)
        )
        if status is not None:
            stmt = stmt.where(Hunt.status == status)
        rows = (await session.execute(stmt)).all()
    return HuntListResponse(hunts=[to_summary(h, name) for h, name in rows])
