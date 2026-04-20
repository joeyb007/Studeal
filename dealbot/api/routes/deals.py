from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Deal

router = APIRouter(prefix="/deals", tags=["deals"])


class DealResponse(BaseModel):
    id: int
    title: str
    source: str
    url: str
    listed_price: float
    sale_price: float
    asin: Optional[str]
    score: int
    alert_tier: str
    category: str
    tags: str
    confidence: str
    real_discount_pct: Optional[float]
    student_eligible: bool
    condition: str
    scraped_at: str

    model_config = {"from_attributes": True}


def _to_response(deal: Deal) -> DealResponse:
    return DealResponse(
        id=deal.id,
        title=deal.title,
        source=deal.source,
        url=deal.url,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        score=deal.score,
        alert_tier=deal.alert_tier,
        category=deal.category,
        tags=deal.tags,
        confidence=deal.confidence,
        real_discount_pct=deal.real_discount_pct,
        student_eligible=deal.student_eligible,
        condition=deal.condition,
        scraped_at=deal.scraped_at.isoformat(),
    )


@router.get("", response_model=list[DealResponse])
async def list_deals(
    tier: Optional[str] = Query(None, description="Filter by alert tier: push, digest, none"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> list[DealResponse]:
    async with get_async_session() as session:
        stmt = (
            select(Deal)
            .where(Deal.hunt_date == date.today())
            .order_by(Deal.scraped_at.desc())
            .offset(offset)
            .limit(limit)
        )
        if tier is not None:
            stmt = stmt.where(Deal.alert_tier == tier)
        result = await session.execute(stmt)
        deals = result.scalars().all()
    return [_to_response(d) for d in deals]


@router.get("/{deal_id}", response_model=DealResponse)
async def get_deal(deal_id: int) -> DealResponse:
    async with get_async_session() as session:
        deal = await session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    return _to_response(deal)
