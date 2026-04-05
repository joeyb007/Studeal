from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import Alert, Deal, User

router = APIRouter(prefix="/alerts", tags=["alerts"])


class AlertResponse(BaseModel):
    id: int
    deal_id: int
    watchlist_id: int
    created_at: str
    deal_title: str
    deal_score: int
    deal_alert_tier: str
    deal_url: str


@router.get("", response_model=list[AlertResponse])
async def list_alerts(
    current_user: User = Depends(get_current_user),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[AlertResponse]:
    async with get_async_session() as session:
        result = await session.execute(
            select(Alert, Deal)
            .join(Deal, Alert.deal_id == Deal.id)
            .where(Alert.user_id == current_user.id)
            .order_by(Alert.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
        rows = result.all()

    return [
        AlertResponse(
            id=alert.id,
            deal_id=alert.deal_id,
            watchlist_id=alert.watchlist_id,
            created_at=alert.created_at.isoformat(),
            deal_title=deal.title,
            deal_score=deal.score,
            deal_alert_tier=deal.alert_tier,
            deal_url=deal.url,
        )
        for alert, deal in rows
    ]
