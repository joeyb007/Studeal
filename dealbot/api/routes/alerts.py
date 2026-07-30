"""Alerts feed routes — the in-app channel of the alert pipeline.

Rows are written by dealbot.worker.alerts; this API reads them joined with
listing + watchlist display fields, and manages read state.
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel
from sqlalchemy import func, select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, ListingAlert, User, Watchlist

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertResponse(BaseModel):
    id: int
    watchlist_id: int
    watchlist_name: str
    listing_id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None
    score: float
    reason: str | None
    created_at: datetime
    read_at: datetime | None


class AlertFeedResponse(BaseModel):
    alerts: list[AlertResponse]
    unread_count: int


@router.get("", response_model=AlertFeedResponse)
async def list_alerts(
    unread_only: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    current_user: User = Depends(get_current_user),
) -> AlertFeedResponse:
    async with get_async_session() as session:
        stmt = (
            select(ListingAlert, Listing, Watchlist.name)
            .join(Listing, ListingAlert.listing_id == Listing.id)
            .join(Watchlist, ListingAlert.watchlist_id == Watchlist.id)
            .where(ListingAlert.user_id == current_user.id)
            .order_by(ListingAlert.created_at.desc())
            .limit(limit)
        )
        if unread_only:
            stmt = stmt.where(ListingAlert.read_at.is_(None))
        rows = (await session.execute(stmt)).all()

        unread_count = (
            await session.execute(
                select(func.count())
                .select_from(ListingAlert)
                .where(
                    ListingAlert.user_id == current_user.id,
                    ListingAlert.read_at.is_(None),
                )
            )
        ).scalar_one()

    return AlertFeedResponse(
        alerts=[
            AlertResponse(
                id=alert.id,
                watchlist_id=alert.watchlist_id,
                watchlist_name=watchlist_name,
                listing_id=listing.id,
                title=listing.title,
                price=listing.price,
                currency=listing.currency,
                marketplace=listing.marketplace,
                url=listing.raw_url,
                image_url=listing.image_url,
                score=alert.score,
                reason=alert.reason,
                created_at=alert.created_at,
                read_at=alert.read_at,
            )
            for alert, listing, watchlist_name in rows
        ],
        unread_count=unread_count,
    )


@router.post("/read-all")
async def read_all(current_user: User = Depends(get_current_user)) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    async with get_async_session() as session:
        rows = (
            await session.execute(
                select(ListingAlert).where(
                    ListingAlert.user_id == current_user.id,
                    ListingAlert.read_at.is_(None),
                )
            )
        ).scalars().all()
        for alert in rows:
            alert.read_at = now
        await session.commit()
    return {"marked": len(rows)}


@router.post("/{alert_id}/read", status_code=status.HTTP_204_NO_CONTENT)
async def mark_read(
    alert_id: int,
    current_user: User = Depends(get_current_user),
) -> Response:
    async with get_async_session() as session:
        alert = await session.get(ListingAlert, alert_id)
        if alert is None or alert.user_id != current_user.id:
            raise HTTPException(status_code=404, detail="Alert not found")
        if alert.read_at is None:
            alert.read_at = datetime.now(timezone.utc)
            await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
