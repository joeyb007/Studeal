from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from dealbot.agents.composition import HuntEventContext, run_hunt
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import HuntFinished, HuntPersisted, HuntStarted
from dealbot.persistence.listings import mark_new_for_watchlist, persist_offers
from dealbot.schemas import WatchlistContext
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

_publisher: RedisEventPublisher | None = None


def _get_publisher() -> RedisEventPublisher:
    """Process-wide lazy singleton ($REDIS_URL). Patchable in tests."""
    global _publisher
    if _publisher is None:
        _publisher = RedisEventPublisher()
    return _publisher


# ---------------------------------------------------------------------------
# research_for_agent — v14. Drives the Explorer/ExtractorPool hunt pipeline
# end-to-end, records the hunt lifecycle (hunts table + live events), persists
# Offers with provenance, and chains alert dispatch for new listings.
# ---------------------------------------------------------------------------

@app.task(name="dealbot.worker.tasks.research_for_agent", bind=True, max_retries=3)
def research_for_agent(self, watchlist_id: int) -> dict:
    """Run the v14 hunt pipeline for a single watchlist; persist listings."""
    try:
        return asyncio.run(_run_hunt_and_persist(watchlist_id))
    except Exception as exc:
        logger.exception("research_for_agent failed for wl=%d: %s", watchlist_id, exc)
        raise self.retry(exc=exc, countdown=2 ** self.request.retries * 60)


async def _run_hunt_and_persist(watchlist_id: int) -> dict:
    # 1. Load watchlist context.
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None:
            logger.warning("research_for_agent: watchlist %d not found", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "not_found"}
        if not watchlist.context:
            logger.warning("research_for_agent: watchlist %d has no context", watchlist_id)
            return {"watchlist_id": watchlist_id, "error": "no_context"}
        context = WatchlistContext.model_validate_json(watchlist.context)

    # 2. Open the hunt: DB row + live event stream identity.
    publisher = _get_publisher()
    async with get_async_session() as session:
        hunt = Hunt(watchlist_id=watchlist_id)
        session.add(hunt)
        await session.commit()
        hunt_id = hunt.id
        started_at = hunt.started_at
    events = HuntEventContext(publisher=publisher, hunt_id=hunt_id, watchlist_id=watchlist_id)
    await publisher.publish(HuntStarted(hunt_id=hunt_id, watchlist_id=watchlist_id))

    governor = _maybe_governor()
    if governor is not None:
        await governor.register(hunt_id)

    try:
        # 3. Run the v14 hunt pipeline. AGENT_BROWSER_BACKEND env var picks
        # Browserbase (production) or local Playwright (dev/eval).
        offers = await run_hunt(context, events=events)

        # 4. Persist offers with hunt provenance; flag alert candidates.
        result = await persist_offers(offers, hunt_id=hunt_id)
        new_ids = await mark_new_for_watchlist(hunt_id)
    except Exception as exc:
        await _close_hunt(
            hunt_id, watchlist_id, started_at,
            status="failed", error=str(exc)[:500], publisher=publisher,
        )
        raise
    finally:
        if governor is not None:
            await governor.deregister(hunt_id)

    # 5. Close the books: hunt row counters + watchlist.last_hunt_at.
    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        hunt.status = "succeeded"
        hunt.finished_at = datetime.now(timezone.utc)
        hunt.offer_count = len(offers)
        hunt.persisted_count = result.written
        hunt.new_listing_count = len(new_ids)
        watchlist = await session.get(Watchlist, watchlist_id)
        watchlist.last_hunt_at = started_at
        await session.commit()

    # 6. Live events + alert chaining.
    await publisher.publish(HuntPersisted(
        hunt_id=hunt_id, watchlist_id=watchlist_id,
        offer_count=len(offers), persisted_count=result.written,
        new_for_watchlist=len(new_ids),
    ))
    await publisher.publish(HuntFinished(
        hunt_id=hunt_id, watchlist_id=watchlist_id,
        status="succeeded", duration_s=_elapsed_s(started_at),
    ))
    if new_ids:
        _maybe_dispatch_alerts(hunt_id)

    logger.info(
        "research_for_agent: wl=%d hunt=%d offers=%d persisted=%d new=%d",
        watchlist_id, hunt_id, len(offers), result.written, len(new_ids),
    )
    return {
        "watchlist_id": watchlist_id,
        "hunt_id": hunt_id,
        "offer_count": len(offers),
        "persisted": result.written,
        "new_for_watchlist": len(new_ids),
    }


async def _close_hunt(
    hunt_id: int,
    watchlist_id: int,
    started_at: datetime,
    *,
    status: str,
    error: str,
    publisher: RedisEventPublisher,
) -> None:
    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is not None:
            hunt.status = status
            hunt.finished_at = datetime.now(timezone.utc)
            hunt.error = error
            await session.commit()
    await publisher.publish(HuntFinished(
        hunt_id=hunt_id, watchlist_id=watchlist_id,
        status="failed", duration_s=_elapsed_s(started_at), error=error,
    ))


def _elapsed_s(started_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:  # sqlite returns stored UTC naive
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds()


def _maybe_governor():
    """FleetGovernor lands in a later task; absent module → no governance."""
    try:
        from dealbot.worker.governor import build_governor
    except ImportError:
        return None
    return build_governor()


def _maybe_dispatch_alerts(hunt_id: int) -> None:
    """dispatch_alerts lands in a later task; absent module → no-op."""
    try:
        from dealbot.worker.alerts import dispatch_alerts
    except ImportError:
        return
    dispatch_alerts.delay(hunt_id)
