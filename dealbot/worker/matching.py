from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError  # raised by begin_nested on conflict
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Alert, Deal, Watchlist, WatchlistKeyword

logger = logging.getLogger(__name__)


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

    # Group keywords by watchlist
    watchlist_map: dict[int, tuple[Watchlist, list[str]]] = {}
    for watchlist, keyword in rows:
        if watchlist.id not in watchlist_map:
            watchlist_map[watchlist.id] = (watchlist, [])
        watchlist_map[watchlist.id][1].append(keyword.keyword)

    search_text = f"{deal.title} {deal.category}".lower()
    alerts_created = 0

    for watchlist, keywords in watchlist_map.values():
        if deal.score < watchlist.min_score:
            continue
        if not any(kw in search_text for kw in keywords):
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
