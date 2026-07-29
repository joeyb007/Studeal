"""dispatch_alerts — turn a hunt's new listings into user alerts.

Candidate flow: this hunt's was_new_for_watchlist listings → hard budget
filter → cross-encoder relevance gate (identity fallback keeps candidates
when Cohere is down) → cap → ListingAlert rows + alert.created events →
one summary email → web push (pro users, when the push module exists).
"""

from __future__ import annotations

import asyncio
import logging
import os

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntListing, Listing, ListingAlert, User, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import AlertCreated
from dealbot.notifications.email import build_alert_email, send_email
from dealbot.rerank.service import RerankService
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

ALERT_SCORE_THRESHOLD = float(os.environ.get("ALERT_SCORE_THRESHOLD", "0.30"))
ALERT_MAX_PER_HUNT = int(os.environ.get("ALERT_MAX_PER_HUNT", "5"))


def _spec_text(context: WatchlistContext) -> str:
    parts = [context.product_query]
    if context.condition:
        parts.append(f"condition: {', '.join(context.condition)}")
    if context.brands:
        parts.append(f"brands: {', '.join(context.brands)}")
    return "; ".join(parts)


def _listing_text(listing: Listing) -> str:
    return (
        f"{listing.title} — {listing.price} {listing.currency}"
        f" — {listing.condition} — {listing.location or ''}"
    )


@app.task(name="dealbot.worker.alerts.dispatch_alerts")
def dispatch_alerts(hunt_id: int) -> dict:
    return asyncio.run(_dispatch(hunt_id))


async def _dispatch(
    hunt_id: int,
    *,
    rerank: RerankService | None = None,
    publisher: RedisEventPublisher | None = None,
) -> dict:
    rerank = rerank or RerankService()
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

        # Relevance gate. Identity fallback (all scores 0.0 — Cohere down or
        # keyless) must not drop everything: keep candidates in given order.
        results = await rerank.rerank(
            _spec_text(context),
            [_listing_text(listing) for listing in candidates],
            top_n=len(candidates),
        )
        if any(r.relevance_score > 0.0 for r in results):
            results = [r for r in results if r.relevance_score >= ALERT_SCORE_THRESHOLD]
            results.sort(key=lambda r: r.relevance_score, reverse=True)
        selected = [
            (candidates[r.index], r.relevance_score)
            for r in results[:ALERT_MAX_PER_HUNT]
        ]
        if not selected:
            return empty

        alerts: list[ListingAlert] = []
        for listing, score in selected:
            alert = ListingAlert(
                user_id=user.id, watchlist_id=watchlist.id,
                listing_id=listing.id, hunt_id=hunt_id, score=score,
            )
            session.add(alert)
            alerts.append(alert)
        await session.commit()

        for alert, (listing, _score) in zip(alerts, selected):
            await publisher.publish(AlertCreated(
                hunt_id=hunt_id, watchlist_id=watchlist.id,
                alert_id=alert.id, listing_id=listing.id,
                title=listing.title, price=listing.price,
                currency=listing.currency, score=alert.score,
                url=listing.raw_url,
            ))

        # One summary email per hunt.
        subject, body = build_alert_email(
            watchlist.name, [(a, listing) for a, (listing, _s) in zip(alerts, selected)],
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
    for _alert, (listing, _score) in zip(alerts, selected):
        delivered = await send_push_to_user(user.id, PushPayload(
            title=f"New match: {watchlist.name}",
            body=f"{listing.title} — ${listing.price:.2f} {listing.currency}",
            url=listing.raw_url,
        ))
        if delivered > 0:
            sent += 1
    return sent
