from __future__ import annotations

import logging
import math

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError  # raised by begin_nested on conflict
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Alert, Deal, Watchlist, WatchlistKeyword

logger = logging.getLogger(__name__)

# Cosine distance threshold — deals within this distance are considered a match.
# 0 = identical, 1 = orthogonal, 2 = opposite. 0.35 catches close paraphrases.
_SIMILARITY_THRESHOLD = 0.35


def _cosine_distance(a: list[float], b: list[float]) -> float:
    """Cosine distance between two equal-length vectors (1 - cosine similarity)."""
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 1.0
    return 1.0 - (dot / (mag_a * mag_b))


def _keyword_matches(
    deal: Deal,
    keyword: WatchlistKeyword,
) -> bool:
    """Return True if the deal is semantically close to the keyword.

    Uses cosine distance when both embeddings are present.
    Falls back to substring match when either embedding is missing
    (e.g. Ollama was down at creation time, or legacy keywords).
    """
    if deal.embedding and keyword.embedding:
        return _cosine_distance(deal.embedding, keyword.embedding) <= _SIMILARITY_THRESHOLD
    # Graceful fallback
    search_text = f"{deal.title} {deal.category}".lower()
    return keyword.keyword in search_text


async def run_matching(deal: Deal, session: AsyncSession) -> int:
    """
    Match a newly persisted deal against all watchlists.
    Writes one Alert row per matched watchlist (deduplicated by uq_alerts_user_deal).
    Returns the number of alerts created.
    """
    # Load all watchlists with their keywords in one query
    result = await session.execute(
        select(Watchlist, WatchlistKeyword)
        .join(WatchlistKeyword, WatchlistKeyword.watchlist_id == Watchlist.id)
    )
    rows = result.all()

    # Group WatchlistKeyword objects by watchlist
    watchlist_map: dict[int, tuple[Watchlist, list[WatchlistKeyword]]] = {}
    for watchlist, keyword in rows:
        if watchlist.id not in watchlist_map:
            watchlist_map[watchlist.id] = (watchlist, [])
        watchlist_map[watchlist.id][1].append(keyword)

    alerts_created = 0

    for watchlist, keywords in watchlist_map.values():
        if deal.score < watchlist.min_score:
            continue
        if not any(_keyword_matches(deal, kw) for kw in keywords):
            continue

        try:
            async with session.begin_nested():  # savepoint — only this insert rolls back on conflict
                session.add(Alert(
                    user_id=watchlist.user_id,
                    deal_id=deal.id,
                    watchlist_id=watchlist.id,
                ))
            alerts_created += 1
            logger.info(
                "matching: alert created user_id=%d deal_id=%d watchlist='%s'",
                watchlist.user_id, deal.id, watchlist.name,
            )
        except IntegrityError:
            logger.debug("matching: duplicate alert skipped user_id=%d deal_id=%d", watchlist.user_id, deal.id)

    if alerts_created:
        await session.commit()

    return alerts_created
