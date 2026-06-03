from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel
from sqlalchemy import select, text

from dealbot.api.auth import get_current_user
from dealbot.api.limiter import limiter
from dealbot.db.database import get_async_session
from dealbot.db.models import Deal, User
from dealbot.llm.embeddings import embed_text

router = APIRouter(prefix="/deals", tags=["deals"])


class DealResponse(BaseModel):
    id: int
    title: str
    source: str
    url: Optional[str]
    affiliate_url: Optional[str] = None
    listed_price: float
    sale_price: float
    asin: Optional[str]
    deal_score: Optional[int]
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
        affiliate_url=deal.affiliate_url,
        listed_price=deal.listed_price,
        sale_price=deal.sale_price,
        asin=deal.asin,
        deal_score=deal.deal_score,
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
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    _: User = Depends(get_current_user),
) -> list[DealResponse]:
    async with get_async_session() as session:
        stmt = (
            select(Deal)
            .order_by(Deal.deal_score.desc().nulls_last(), Deal.scraped_at.desc())
            .offset(offset)
            .limit(limit)
        )
        result = await session.execute(stmt)
        deals = result.scalars().all()
    return [_to_response(d) for d in deals]


@router.get("/search", response_model=list[DealResponse])
@limiter.limit("30/minute")
async def search_deals(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(20, ge=1, le=100),
    _: User = Depends(get_current_user),
) -> list[DealResponse]:
    embedding = await embed_text(q)

    async with get_async_session() as session:
        if embedding:
            # Semantic search via cosine similarity
            result = await session.execute(
                text(
                    "SELECT * FROM deals WHERE embedding IS NOT NULL "
                    "ORDER BY embedding <=> CAST(:emb AS vector) "
                    "LIMIT :limit"
                ),
                {"emb": str(embedding), "limit": limit},
            )
            rows = result.mappings().all()
            deals = [Deal(**dict(row)) for row in rows]
        else:
            # Fallback: title keyword search
            result = await session.execute(
                select(Deal)
                .where(Deal.title.ilike(f"%{q}%"))
                .order_by(Deal.deal_score.desc().nulls_last())
                .limit(limit)
            )
            deals = list(result.scalars().all())

    return [_to_response(d) for d in deals]


@router.get("/{deal_id}", response_model=DealResponse)
async def get_deal(deal_id: int, _: User = Depends(get_current_user)) -> DealResponse:
    async with get_async_session() as session:
        deal = await session.get(Deal, deal_id)
    if deal is None:
        raise HTTPException(status_code=404, detail="Deal not found")
    return _to_response(deal)
