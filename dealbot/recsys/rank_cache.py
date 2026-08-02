"""The precomputed ranking cache — recsys output as rows, not requests.

The read path previously ran the full pipeline live per view: pgvector
retrieval + ~6 windowed LLM calls = 3-8s of request latency for a result
identical to the last view unless something changed. Rankings are now
computed here — on hunt completion, on context edits, and lazily when stale —
and the endpoint serves rows in ~50ms. The LLM never runs on a request path.

Everything retrieved gets ranked and persisted (not a top-N): once compute is
off the request path, returning the full ordered continuum costs nothing, and
the UI can show the quality decay instead of us guessing a cutoff.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select

from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, Watchlist, WatchlistRanking
from dealbot.lifecycle import stale_cutoff
from dealbot.recsys.ranker import rank
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

# Shortlist handed to the ranker. Beyond this depth of τ-similarity the tail
# is noise, not inventory.
CANDIDATE_POOL_SIZE = 150

# Lazy backstop: rankings older than this are re-enqueued on read (served
# stale meanwhile). Own hunts and edits recompute immediately — this only
# catches pool drift from OTHER agents' hunts.
RANKINGS_STALE_HOURS = 12


async def _candidates(watchlist: Watchlist, context: WatchlistContext | None) -> list[Listing]:
    """Same contract the live read path had: hard constraints in SQL, dense
    retrieval when an intent vector exists, recency fallback when not."""
    async with get_async_session() as session:
        stmt = select(Listing)
        stmt = stmt.where(Listing.last_seen_at >= stale_cutoff())
        if context is not None:
            if context.max_budget:
                stmt = stmt.where(Listing.price <= context.max_budget * 1.2)
            if context.condition:
                stmt = stmt.where(Listing.condition.in_(context.condition))

        if watchlist.intent_embedding is not None:
            stmt = stmt.where(Listing.embedding.isnot(None)).order_by(
                Listing.embedding.cosine_distance(watchlist.intent_embedding)
            )
        else:
            stmt = stmt.order_by(Listing.last_seen_at.desc())
        return list(
            (await session.execute(stmt.limit(CANDIDATE_POOL_SIZE))).scalars().all()
        )


async def recompute_rankings(watchlist_id: int) -> int:
    """Retrieve, rank everything, replace this watchlist's rows. Returns the
    number of rankings written (0 clears the cache — an honest empty)."""
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None:
            return 0
        context = (
            WatchlistContext.model_validate_json(watchlist.context)
            if watchlist.context else None
        )

    candidates = await _candidates(watchlist, context)
    ranked = (
        await rank(context, candidates, top_n=len(candidates))
        if candidates and context is not None
        else []
    )

    now = datetime.now(timezone.utc)
    async with get_async_session() as session:
        await session.execute(
            delete(WatchlistRanking).where(
                WatchlistRanking.watchlist_id == watchlist_id
            )
        )
        for position, entry in enumerate(ranked):
            session.add(WatchlistRanking(
                watchlist_id=watchlist_id, listing_id=entry.listing.id,
                score=entry.score, reason=entry.reason or None,
                position=position, computed_at=now,
            ))
        await session.commit()

    logger.info("recompute_rankings: wl=%d wrote %d", watchlist_id, len(ranked))
    return len(ranked)


def rankings_are_stale(computed_at: datetime | None) -> bool:
    if computed_at is None:
        return True
    if computed_at.tzinfo is None:
        computed_at = computed_at.replace(tzinfo=timezone.utc)
    age_limit = datetime.now(timezone.utc) - timedelta(hours=RANKINGS_STALE_HOURS)
    return computed_at < age_limit
