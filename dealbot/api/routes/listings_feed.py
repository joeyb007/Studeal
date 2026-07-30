"""Pool read surface — Daily Drops over the cumulative agent listings pool.

Two paths:
  - `/listings/feed`   — diversified browse (no query)
  - `/listings/search` — natural-language semantic search

Ranking is by relevance (search) or freshness+diversity (feed), never a
fabricated "deal score": pool listings have no reference price until the
price-intelligence layer accumulates history.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel
from sqlalchemy import select

from dealbot.api.auth import get_current_user
from dealbot.api.limiter import limiter
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntListing, Listing, User
from dealbot.llm.embeddings import embed_text

router = APIRouter(prefix="/listings", tags=["pool"])

# A single hunt writes ~100 near-identical listings at once. Without a cap the
# newest hunt owns the whole first page and the pool looks one-note.
MAX_CONSECUTIVE_PER_MARKETPLACE = 3

# Upper bound on rows pulled before diversification. Large enough that paging
# through a few screens stays stable, small enough to keep the query cheap.
_DIVERSIFY_POOL_SIZE = 1000


class PoolListingResponse(BaseModel):
    id: int
    title: str
    price: float
    currency: str
    marketplace: str
    url: str
    image_url: str | None
    location: str | None
    condition: str
    first_seen_at: datetime
    last_seen_at: datetime
    relevance: float | None = None   # search only


class PoolFeedResponse(BaseModel):
    listings: list[PoolListingResponse]
    total_in_window: int


class PoolSearchResponse(BaseModel):
    listings: list[PoolListingResponse]
    semantic: bool                   # False → keyword fallback ran


def _to_pool_response(listing: Listing, relevance: float | None = None) -> PoolListingResponse:
    return PoolListingResponse(
        id=listing.id,
        title=listing.title,
        price=listing.price,
        currency=listing.currency,
        marketplace=listing.marketplace,
        url=listing.raw_url,
        image_url=listing.image_url,
        location=listing.location,
        condition=listing.condition,
        first_seen_at=listing.first_seen_at,
        last_seen_at=listing.last_seen_at,
        relevance=relevance,
    )


def _interleave(
    buckets: dict[tuple[str, int | None], list[Listing]],
    max_run: int = MAX_CONSECUTIVE_PER_MARKETPLACE,
) -> list[Listing]:
    """Round-robin across (marketplace, source watchlist) buckets so one bursty
    hunt cannot fill the page.

    The run cap is on MARKETPLACE, not bucket: capping per bucket would still
    allow 6 consecutive Kijiji rows from two different watchlists, and
    marketplace is what a user perceives as repetition.

    Deterministic — buckets are visited in sorted order, so paging by offset
    stays stable across requests.
    """
    queues = [
        list(buckets[key])
        for key in sorted(buckets, key=lambda k: (k[0], k[1] if k[1] is not None else -1))
    ]
    out: list[Listing] = []
    last_marketplace: str | None = None
    run = 0

    while any(queues):
        progressed = False
        for queue in queues:
            if not queue:
                continue
            candidate = queue[0]
            if candidate.marketplace == last_marketplace and run >= max_run:
                continue
            queue.pop(0)
            out.append(candidate)
            run = run + 1 if candidate.marketplace == last_marketplace else 1
            last_marketplace = candidate.marketplace
            progressed = True
        if not progressed:
            # Everything left is blocked by the run cap (only one marketplace
            # remains). Emit it rather than dropping listings.
            for queue in queues:
                if queue:
                    out.append(queue.pop(0))
                    last_marketplace, run = out[-1].marketplace, 1
                    break
    return out


@router.get("/feed", response_model=PoolFeedResponse)
async def pool_feed(
    limit: int = Query(40, ge=1, le=100),
    offset: int = Query(0, ge=0),
    marketplace: str | None = Query(None),
    max_price: float | None = Query(None, gt=0),
    condition: str | None = Query(None),
    days: int = Query(7, ge=1, le=90),
    current_user: User = Depends(get_current_user),
) -> PoolFeedResponse:
    """Diversified browse over everything the fleet has surfaced recently."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_async_session() as session:
        stmt = (
            select(Listing, Hunt.watchlist_id)
            .outerjoin(HuntListing, HuntListing.listing_id == Listing.id)
            .outerjoin(Hunt, Hunt.id == HuntListing.hunt_id)
            .where(Listing.last_seen_at >= cutoff)
            .order_by(Listing.last_seen_at.desc())
            .limit(_DIVERSIFY_POOL_SIZE)
        )
        if marketplace:
            stmt = stmt.where(Listing.marketplace == marketplace)
        if max_price is not None:
            stmt = stmt.where(Listing.price <= max_price)
        if condition:
            stmt = stmt.where(Listing.condition == condition)
        rows = (await session.execute(stmt)).all()

    seen_ids: set[int] = set()
    buckets: dict[tuple[str, int | None], list[Listing]] = {}
    for listing, watchlist_id in rows:
        # A listing can link to several hunts; keep its first (newest) row.
        if listing.id in seen_ids:
            continue
        seen_ids.add(listing.id)
        buckets.setdefault((listing.marketplace, watchlist_id), []).append(listing)

    ordered = _interleave(buckets)
    page = ordered[offset:offset + limit]
    return PoolFeedResponse(
        listings=[_to_pool_response(row) for row in page],
        total_in_window=len(ordered),
    )


@router.get("/search", response_model=PoolSearchResponse)
@limiter.limit("30/minute")
async def pool_search(
    request: Request,
    q: str = Query(..., min_length=1),
    limit: int = Query(40, ge=1, le=100),
    marketplace: str | None = Query(None),
    max_price: float | None = Query(None, gt=0),
    condition: str | None = Query(None),
    days: int = Query(30, ge=1, le=90),
    current_user: User = Depends(get_current_user),
) -> PoolSearchResponse:
    """Natural-language search: the query IS the intent profile. Structured
    filters narrow the candidate set, then vector similarity ranks it."""
    embedding = await embed_text(q)
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with get_async_session() as session:
        base = select(Listing).where(Listing.last_seen_at >= cutoff)
        if marketplace:
            base = base.where(Listing.marketplace == marketplace)
        if max_price is not None:
            base = base.where(Listing.price <= max_price)
        if condition:
            base = base.where(Listing.condition == condition)

        if embedding:
            stmt = (
                base.where(Listing.embedding.is_not(None))
                .order_by(Listing.embedding.cosine_distance(embedding))
                .limit(limit)
            )
            listings = list((await session.execute(stmt)).scalars().all())
            return PoolSearchResponse(
                listings=[_to_pool_response(row) for row in listings],
                semantic=True,
            )

        # No embedding (backend down or unconfigured): keyword fallback, and
        # say so rather than pretending the ranking was semantic.
        stmt = (
            base.where(Listing.title.ilike(f"%{q}%"))
            .order_by(Listing.last_seen_at.desc())
            .limit(limit)
        )
        listings = list((await session.execute(stmt)).scalars().all())

    return PoolSearchResponse(
        listings=[_to_pool_response(row) for row in listings],
        semantic=False,
    )
