from __future__ import annotations

import logging

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Deal

logger = logging.getLogger(__name__)

_DEFAULT_K = 3


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
