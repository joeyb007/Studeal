from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User, Watchlist, WatchlistKeyword

router = APIRouter(prefix="/watchlists", tags=["watchlists"])


class WatchlistCreate(BaseModel):
    name: str
    keywords: list[str]
    min_score: int = 50
    alert_tier_threshold: str = "digest"


class WatchlistResponse(BaseModel):
    id: int
    name: str
    keywords: list[str]
    min_score: int
    alert_tier_threshold: str


@router.post("", response_model=WatchlistResponse, status_code=status.HTTP_201_CREATED)
async def create_watchlist(
    body: WatchlistCreate,
    current_user: User = Depends(get_current_user),
) -> WatchlistResponse:
    async with get_async_session() as session:
        watchlist = Watchlist(
            user_id=current_user.id,
            name=body.name,
            min_score=body.min_score,
            alert_tier_threshold=body.alert_tier_threshold,
        )
        session.add(watchlist)
        await session.flush()  # get watchlist.id before adding keywords

        for kw in body.keywords:
            session.add(WatchlistKeyword(watchlist_id=watchlist.id, keyword=kw.lower().strip()))

        await session.commit()
        await session.refresh(watchlist)

    return WatchlistResponse(
        id=watchlist.id,
        name=watchlist.name,
        keywords=body.keywords,
        min_score=watchlist.min_score,
        alert_tier_threshold=watchlist.alert_tier_threshold,
    )


@router.get("", response_model=list[WatchlistResponse])
async def list_watchlists(
    current_user: User = Depends(get_current_user),
) -> list[WatchlistResponse]:
    async with get_async_session() as session:
        result = await session.execute(
            select(Watchlist).where(Watchlist.user_id == current_user.id)
        )
        watchlists = result.scalars().all()

        responses = []
        for wl in watchlists:
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
            ))

    return responses
