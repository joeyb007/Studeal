"""Semantic dedup helpers for the research agent.

Two-layer pattern:
- Layer 1 (find_recent_similar_query): query→query — skip external API if a
  semantically similar query was issued recently.
- Layer 2 (retrieve_similar_deals): query→deal — always retrieve from the
  global pool of persisted deals. Prior research compounds across users.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Deal, HuntQuery

logger = logging.getLogger(__name__)

LAYER1_THRESHOLD = 0.92  # cosine similarity — anything above is "same query"
LAYER1_MAX_AGE_HOURS = 24
LAYER2_THRESHOLD = 0.85  # cosine similarity — pool deals that match this query
LAYER2_LIMIT = 15


def _vector_literal(embedding: list[float]) -> str:
    return "[" + ",".join(str(v) for v in embedding) + "]"


async def find_recent_similar_query(
    embedding: list[float],
    session: AsyncSession,
    threshold: float = LAYER1_THRESHOLD,
    max_age_hours: int = LAYER1_MAX_AGE_HOURS,
) -> HuntQuery | None:
    """Layer 1 — return a recent HuntQuery whose embedding is cosine-similar to
    the input embedding above the threshold, or None if no match.

    pgvector `<=>` returns cosine distance (1 - similarity). We want similarity
    > threshold, which is distance < (1 - threshold).
    """
    if embedding is None or len(embedding) == 0:
        return None

    distance_cutoff = 1.0 - threshold
    cutoff_ts = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    emb_lit = _vector_literal(embedding)

    try:
        result = await session.execute(
            select(HuntQuery)
            .where(HuntQuery.embedding.isnot(None))
            .where(HuntQuery.hunt_timestamp > cutoff_ts)
            .where(text("embedding <=> CAST(:emb AS vector) < :cutoff")
                   .bindparams(emb=emb_lit, cutoff=distance_cutoff))
            .order_by(text("embedding <=> CAST(:emb AS vector)")
                      .bindparams(emb=emb_lit))
            .limit(1)
        )
        return result.scalars().first()
    except Exception:
        logger.exception("find_recent_similar_query: pgvector query failed")
        return None


async def retrieve_similar_deals(
    embedding: list[float],
    session: AsyncSession,
    threshold: float = LAYER2_THRESHOLD,
    k: int = LAYER2_LIMIT,
) -> list[Deal]:
    """Layer 2 — return the k most cosine-similar persisted deals above threshold.

    Used during research to enrich the agent's accumulated results with deals
    already in the pool. The DB itself becomes the long-term memory.
    """
    if embedding is None or len(embedding) == 0:
        return []

    distance_cutoff = 1.0 - threshold
    emb_lit = _vector_literal(embedding)

    try:
        result = await session.execute(
            select(Deal)
            .where(Deal.embedding.isnot(None))
            .where(text("embedding <=> CAST(:emb AS vector) < :cutoff")
                   .bindparams(emb=emb_lit, cutoff=distance_cutoff))
            .order_by(text("embedding <=> CAST(:emb AS vector)")
                      .bindparams(emb=emb_lit))
            .limit(k)
        )
        return list(result.scalars().all())
    except Exception:
        logger.exception("retrieve_similar_deals: pgvector query failed")
        return []


async def persist_hunt_query(
    watchlist_id: int,
    query_text: str,
    embedding: list[float],
    cost_usd: float,
    deal_ids: list[int],
    session: AsyncSession,
) -> HuntQuery:
    """Write a HuntQuery row and link it to the deals it produced."""
    hq = HuntQuery(
        watchlist_id=watchlist_id,
        query_text=query_text,
        embedding=embedding or None,
        cost_usd=cost_usd,
    )
    session.add(hq)
    await session.flush()

    if deal_ids:
        rows = [
            {"hunt_query_id": hq.id, "deal_id": did} for did in deal_ids
        ]
        from dealbot.db.models import hunt_query_deals
        await session.execute(hunt_query_deals.insert(), rows)

    return hq
