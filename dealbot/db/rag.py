from __future__ import annotations

import logging
from datetime import date

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Deal

logger = logging.getLogger(__name__)

_DEFAULT_K = 3
_DEDUP_THRESHOLD = 0.35


def _cosine_distance(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = sum(x * x for x in a) ** 0.5
    norm_b = sum(x * x for x in b) ** 0.5
    if norm_a == 0 or norm_b == 0:
        return 1.0
    return 1.0 - dot / (norm_a * norm_b)


async def retrieve_similar_deals(
    embedding: list[float],
    session: AsyncSession,
    k: int = _DEFAULT_K,
) -> list[Deal]:
    """Return the k most similar persisted deals by cosine distance (pgvector).

    Requires the deals table to have a populated `embedding` column.
    Returns an empty list on any failure so callers degrade gracefully.
    """
    if not embedding:
        return []

    # Format the vector literal for pgvector's <=> cosine distance operator
    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    try:
        result = await session.execute(
            select(Deal)
            .where(Deal.embedding.isnot(None))
            .order_by(
                text("embedding <=> CAST(:emb AS vector)").bindparams(
                    emb=embedding_literal
                )
            )
            .limit(k)
        )
        return list(result.scalars().all())
    except Exception:
        logger.exception("retrieve_similar_deals: pgvector query failed")
        return []


async def keyword_covered_today(
    embedding: list[float],
    session: AsyncSession,
    threshold: float = _DEDUP_THRESHOLD,
) -> bool:
    """Return True if a semantically similar keyword was already searched today.

    Checks whether any deal persisted today has a cosine distance to the given
    embedding within threshold. Used to skip redundant Brave searches.
    """
    if not embedding:
        return False

    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    try:
        result = await session.execute(
            select(Deal)
            .where(Deal.embedding.isnot(None))
            .where(Deal.hunt_date == date.today())
            .order_by(
                text("embedding <=> CAST(:emb AS vector)").bindparams(
                    emb=embedding_literal
                )
            )
            .limit(1)
        )
        closest = result.scalars().first()
        if closest is None or closest.embedding is None:
            return False

        distance = _cosine_distance(closest.embedding, embedding)
        covered = distance <= threshold
        if covered:
            logger.info("keyword_covered_today: skipping, closest deal distance=%.3f", distance)
        return covered
    except Exception:
        logger.exception("keyword_covered_today: pgvector query failed, defaulting to not covered")
        return False
