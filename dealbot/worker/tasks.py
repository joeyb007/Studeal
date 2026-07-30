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

REQUEUE_DELAY_S = 60


class FleetAtCapacity(Exception):
    """Raised pre-hunt when the fleet is at its concurrency cap; the task
    re-enqueues itself with a delay instead of consuming failure retries."""


_publisher_cache: dict[str, object] = {"loop": None, "publisher": None}


def _get_publisher() -> RedisEventPublisher:
    """Per-event-loop publisher ($REDIS_URL). Celery gives every task a fresh
    loop; a redis client bound to a finished loop poisons later tasks. Keyed
    by loop identity with a held reference (see dealbot.db.database).
    Patchable in tests."""
    loop = asyncio.get_running_loop()
    if _publisher_cache["loop"] is not loop:
        _publisher_cache["loop"] = loop
        _publisher_cache["publisher"] = RedisEventPublisher()
    return _publisher_cache["publisher"]  # type: ignore[return-value]


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
    except FleetAtCapacity:
        logger.info(
            "research_for_agent: fleet at capacity — requeueing wl=%d in %ds",
            watchlist_id, REQUEUE_DELAY_S,
        )
        research_for_agent.apply_async(args=[watchlist_id], countdown=REQUEUE_DELAY_S)
        return {"watchlist_id": watchlist_id, "requeued": True}
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

    # 2. Capacity gate — BEFORE any row exists, so a requeue leaves no orphan.
    # Governor errors fail open: a Redis blip must not stop the fleet.
    governor = _maybe_governor()
    if governor is not None:
        try:
            at_capacity = not await governor.has_capacity()
        except Exception:
            logger.warning("governor capacity check failed — failing open", exc_info=True)
            at_capacity = False
        if at_capacity:
            raise FleetAtCapacity(f"wl={watchlist_id}")

    # 3. Open the hunt: DB row + live event stream identity.
    publisher = _get_publisher()
    async with get_async_session() as session:
        hunt = Hunt(watchlist_id=watchlist_id)
        session.add(hunt)
        await session.commit()
        hunt_id = hunt.id
        started_at = hunt.started_at
    events = HuntEventContext(publisher=publisher, hunt_id=hunt_id, watchlist_id=watchlist_id)
    await publisher.publish(HuntStarted(hunt_id=hunt_id, watchlist_id=watchlist_id))

    if governor is not None:
        try:
            await governor.register(hunt_id)
        except Exception:
            logger.warning("governor register failed for hunt %d", hunt_id, exc_info=True)

    try:
        # 4. Run the v14 hunt pipeline. AGENT_BROWSER_BACKEND env var picks
        # Browserbase (production) or local Playwright (dev/eval).
        offers = await run_hunt(context, events=events)

        # 5. Persist offers with hunt provenance; flag alert candidates.
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
            try:
                await governor.deregister(hunt_id)
            except Exception:
                # Must never mask an in-flight hunt exception.
                logger.warning("governor deregister failed for hunt %d", hunt_id, exc_info=True)

    # 6. Close the books: hunt row counters + watchlist.last_hunt_at.
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

    # 7. Live events + alert chaining.
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
        status=status, duration_s=_elapsed_s(started_at), error=error,
    ))


def _elapsed_s(started_at: datetime) -> float:
    now = datetime.now(timezone.utc)
    if started_at.tzinfo is None:  # sqlite returns stored UTC naive
        started_at = started_at.replace(tzinfo=timezone.utc)
    return (now - started_at).total_seconds()


def _maybe_governor():
    """Seam for tests (patched to None); production always governs."""
    from dealbot.worker.governor import build_governor

    return build_governor()


def _maybe_dispatch_alerts(hunt_id: int) -> None:
    """Seam for tests; production chains alert dispatch off every fruitful hunt.
    A broker blip here must not fail the (already successful, already paid-for)
    hunt — the listings are persisted; alerts surface on the next hunt."""
    from dealbot.worker.alerts import dispatch_alerts

    try:
        dispatch_alerts.delay(hunt_id)
    except Exception:
        logger.warning("alert dispatch enqueue failed for hunt %d", hunt_id, exc_info=True)
