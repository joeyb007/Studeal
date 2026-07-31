"""dispatch_alerts — turn a hunt's new listings into user alerts.

Candidate flow: this hunt's was_new_for_watchlist listings → hard budget
filter → listwise LLM rank with a reason per listing (all-zero scores mean
every window failed, so the threshold is skipped rather than dropping
everything) → cap → ListingAlert rows + alert.created events → one summary
email → web push (pro users, when the push module exists).

Candidates are provenance-scoped — the listings THIS hunt found for THIS
watchlist. Dense retrieval over the shared pool belongs on the read path,
where it can surface what other users' agents found; here it would only add
machinery, since these candidates are already topically relevant by
construction.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntListing, Listing, ListingAlert, User, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import AlertCreated
from dealbot.notifications.email import build_alert_email, send_email
from dealbot.recsys.ranker import RankedListing, rank
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

ALERT_SCORE_THRESHOLD = float(os.environ.get("ALERT_SCORE_THRESHOLD", "0.30"))
ALERT_MAX_PER_HUNT = int(os.environ.get("ALERT_MAX_PER_HUNT", "5"))


@app.task(name="dealbot.worker.alerts.dispatch_alerts")
def dispatch_alerts(hunt_id: int) -> dict:
    return asyncio.run(_dispatch(hunt_id))


async def _dispatch(
    hunt_id: int,
    *,
    ranker: Callable[..., Awaitable[list[RankedListing]]] | None = None,
    publisher: RedisEventPublisher | None = None,
) -> dict:
    ranker = ranker or rank
    publisher = publisher or RedisEventPublisher()
    empty = {"alerts": 0, "emailed": False, "pushed": 0}

    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is None:
            logger.warning("dispatch_alerts: hunt %d not found", hunt_id)
            return empty
        watchlist = await session.get(Watchlist, hunt.watchlist_id)
        user = await session.get(User, watchlist.user_id)
        if not watchlist.context:
            return empty
        context = WatchlistContext.model_validate_json(watchlist.context)

        # Candidates: this hunt's new-for-watchlist listings, budget-filtered.
        stmt = (
            select(Listing)
            .join(HuntListing, HuntListing.listing_id == Listing.id)
            .where(
                HuntListing.hunt_id == hunt_id,
                HuntListing.was_new_for_watchlist.is_(True),
            )
        )
        if context.max_budget is not None:
            stmt = stmt.where(Listing.price <= context.max_budget)
        candidates = (await session.execute(stmt)).scalars().all()
        if not candidates:
            return empty

        # Relevance gate. rank() never returns empty for non-empty candidates,
        # but it CAN return all-zero scores when every window failed — applying
        # the threshold then would drop everything, so skip it.
        ranked = await ranker(context, candidates, top_n=len(candidates))
        if any(r.score > 0.0 for r in ranked):
            ranked = [r for r in ranked if r.score >= ALERT_SCORE_THRESHOLD]
        selected = ranked[:ALERT_MAX_PER_HUNT]
        if not selected:
            return empty

        alerts: list[ListingAlert] = []
        for ranked_listing in selected:
            alert = ListingAlert(
                user_id=user.id, watchlist_id=watchlist.id,
                listing_id=ranked_listing.listing.id, hunt_id=hunt_id,
                score=ranked_listing.score,
                reason=ranked_listing.reason or None,
            )
            session.add(alert)
            alerts.append(alert)
        await session.commit()

        for alert, ranked_listing in zip(alerts, selected):
            listing = ranked_listing.listing
            await publisher.publish(AlertCreated(
                hunt_id=hunt_id, watchlist_id=watchlist.id,
                alert_id=alert.id, listing_id=listing.id,
                title=listing.title, price=listing.price,
                currency=listing.currency, score=alert.score,
                url=listing.raw_url,
            ))

        # One summary email per hunt.
        subject, body = build_alert_email(
            watchlist.name, [(a, r.listing) for a, r in zip(alerts, selected)],
        )
        emailed = await send_email(user.email, subject, body)
        if emailed:
            for alert in alerts:
                alert.channels = f"{alert.channels},email"

        pushed = await _try_push(user, watchlist, alerts, selected)
        if pushed:
            for alert in alerts:
                alert.channels = f"{alert.channels},push"
        await session.commit()

    logger.info(
        "dispatch_alerts: hunt=%d alerts=%d emailed=%s pushed=%d",
        hunt_id, len(alerts), emailed, pushed,
    )
    return {"alerts": len(alerts), "emailed": emailed, "pushed": pushed}


async def _try_push(user, watchlist, alerts, selected) -> int:
    """Web push ships in a later task; absent module or free tier → 0 sends."""
    if not user.is_pro:
        return 0
    try:
        from dealbot.notifications.push import PushPayload, send_push_to_user
    except ImportError:
        return 0
    sent = 0
    for _alert, ranked_listing in zip(alerts, selected):
        listing = ranked_listing.listing
        delivered = await send_push_to_user(user.id, PushPayload(
            title=f"New match: {watchlist.name}",
            body=f"{listing.title} — ${listing.price:.2f} {listing.currency}",
            url=listing.raw_url,
        ))
        if delivered > 0:
            sent += 1
    return sent
