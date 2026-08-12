from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from dealbot.agents.composition import HuntEventContext, run_hunt
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, User, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import HuntFinished, HuntPersisted, HuntStarted
from dealbot.persistence.listings import mark_new_for_watchlist, persist_offers
from dealbot.recsys.gate import GATE_SUFFICIENCY_K, link_pool_candidates, pool_candidates
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
def research_for_agent(self, watchlist_id: int, first_hunt: bool = False) -> dict:
    """Run the v14 hunt pipeline for a single watchlist; persist listings.
    first_hunt keeps its queue priority through capacity requeues."""
    try:
        return asyncio.run(_run_hunt_and_persist(watchlist_id))
    except FleetAtCapacity:
        logger.info(
            "research_for_agent: fleet at capacity — requeueing wl=%d in %ds",
            watchlist_id, REQUEUE_DELAY_S,
        )
        research_for_agent.apply_async(
            args=[watchlist_id], kwargs={"first_hunt": first_hunt},
            countdown=REQUEUE_DELAY_S,
            priority=0 if first_hunt else None,
        )
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
        user = await session.get(User, watchlist.user_id)

    # 1b. Sufficiency gate (non-pro only): if the pool already holds enough
    # fresh, novel matches, serve the refresh from it — no browser, no
    # governor slot. Pro always hunts live; freshness is the paid feature.
    # The house user is pro, so house seeds always hunt fully — they are
    # the pump that keeps the pool fed.
    candidates: list = []
    if user is not None and not user.is_pro:
        candidates = await pool_candidates(watchlist_id)
        if len(candidates) >= GATE_SUFFICIENCY_K:
            return await _serve_from_pool(watchlist_id, candidates)

    # 2a. Spend guards — the break-glass pause and the daily LLM budget both
    # stop background hunting outright (no requeue: today's budget is spent;
    # cadence re-enqueues tomorrow). Checks fail open on Redis errors.
    from dealbot.costs import build_meter, fleet_paused

    if fleet_paused():
        logger.warning("research_for_agent: FLEET_PAUSED — skipping wl=%d", watchlist_id)
        return {"watchlist_id": watchlist_id, "skipped": "fleet_paused"}
    if not await build_meter().llm_budget_ok():
        logger.error(
            "research_for_agent: daily LLM budget reached — skipping wl=%d",
            watchlist_id,
        )
        return {"watchlist_id": watchlist_id, "skipped": "budget"}

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
        # embed=False: vectors are filled by the embed consumer task below,
        # so the browse result lands without a serial embedding tail.
        result = await persist_offers(offers, hunt_id=hunt_id, embed=False)
        # Pool-first alerts, both branches: even below k, matches from other
        # agents' finds ride along as alert candidates.
        if candidates:
            await link_pool_candidates(hunt_id, [c.id for c in candidates])
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
    # Alerts and rankings both gate on cosine similarity, so the whole chain
    # rides behind the embed consumer — it fires the same three hooks once
    # this hunt's vectors exist.
    _maybe_embed_then_chain(hunt_id, watchlist_id, has_new=bool(new_ids))

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


async def _serve_from_pool(watchlist_id: int, candidates: list) -> dict:
    """A refresh answered entirely by other agents' finds.

    Same bookkeeping contract as a real hunt — Hunt row, events, novelty,
    alerts, last_hunt_at — with zero browsing. Alerts are never cached: the
    novelty → rank → alert chain runs exactly as it would post-hunt.
    """
    publisher = _get_publisher()
    async with get_async_session() as session:
        hunt = Hunt(watchlist_id=watchlist_id)
        session.add(hunt)
        await session.commit()
        hunt_id = hunt.id
        started_at = hunt.started_at
    await publisher.publish(HuntStarted(hunt_id=hunt_id, watchlist_id=watchlist_id))

    linked = await link_pool_candidates(hunt_id, [c.id for c in candidates])
    new_ids = await mark_new_for_watchlist(hunt_id)

    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        hunt.status = "cached"
        hunt.finished_at = datetime.now(timezone.utc)
        hunt.offer_count = 0                    # offer_count means BROWSED offers
        hunt.persisted_count = 0
        hunt.new_listing_count = len(new_ids)
        watchlist = await session.get(Watchlist, watchlist_id)
        watchlist.last_hunt_at = started_at     # cadence resets — this WAS the refresh
        await session.commit()

    await publisher.publish(HuntPersisted(
        hunt_id=hunt_id, watchlist_id=watchlist_id,
        offer_count=0, persisted_count=0, new_for_watchlist=len(new_ids),
    ))
    await publisher.publish(HuntFinished(
        hunt_id=hunt_id, watchlist_id=watchlist_id,
        status="cached", duration_s=_elapsed_s(started_at),
    ))
    if new_ids:
        _maybe_dispatch_alerts(hunt_id)
    _maybe_recompute_rankings(watchlist_id)
    _maybe_post_hunt_inspections(hunt_id, watchlist_id)

    logger.info(
        "research_for_agent: wl=%d hunt=%d CACHED linked=%d new=%d",
        watchlist_id, hunt_id, linked, len(new_ids),
    )
    return {
        "watchlist_id": watchlist_id, "hunt_id": hunt_id,
        "cached": True, "linked": linked, "new": len(new_ids),
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


@app.task(name="dealbot.worker.tasks.embed_hunt_listings_task")
def embed_hunt_listings_task(hunt_id: int, watchlist_id: int, has_new: bool) -> dict:
    """Embed consumer: fill this hunt's NULL vectors, then fire the post-hunt
    chain (alerts / rankings / inspections) that depends on them. The chain
    fires even if embedding partially fails — the healer sweep backfills."""
    from dealbot.persistence.listings import embed_pending_for_hunt

    try:
        embedded = asyncio.run(embed_pending_for_hunt(hunt_id))
    except Exception:
        logger.exception("embed consumer failed for hunt %d", hunt_id)
        embedded = 0
    if has_new:
        _maybe_dispatch_alerts(hunt_id)
    _maybe_recompute_rankings(watchlist_id)
    _maybe_post_hunt_inspections(hunt_id, watchlist_id)
    return {"hunt_id": hunt_id, "embedded": embedded}


def _maybe_embed_then_chain(hunt_id: int, watchlist_id: int, *, has_new: bool) -> None:
    """Fire-and-forget; a dead broker must not fail the hunt. Falls back to
    chaining directly so alerts/rankings never silently stall."""
    try:
        embed_hunt_listings_task.delay(hunt_id, watchlist_id, has_new)
    except Exception:
        logger.warning("embed consumer enqueue failed for hunt %d", hunt_id)
        if has_new:
            _maybe_dispatch_alerts(hunt_id)
        _maybe_recompute_rankings(watchlist_id)
        _maybe_post_hunt_inspections(hunt_id, watchlist_id)


@app.task(name="dealbot.worker.tasks.embed_orphan_listings_task")
def embed_orphan_listings_task() -> dict:
    """Beat healer: backfill vectors for any live listing that missed the
    consumer pass (crashed task, transient embedding outage)."""
    from dealbot.persistence.listings import embed_orphan_listings

    return {"embedded": asyncio.run(embed_orphan_listings())}


@app.task(name="dealbot.worker.tasks.recompute_rankings_task")
def recompute_rankings_task(watchlist_id: int) -> dict:
    from dealbot.recsys.rank_cache import recompute_rankings
    from dealbot.worker.inspections import enforce_quality_bar

    async def _run() -> dict:
        written = await recompute_rankings(watchlist_id)
        # Recomputes rewrite positions from the ranker, erasing any earlier
        # quality demotions — re-apply the owner's bar over cached flags.
        try:
            demoted = await enforce_quality_bar(watchlist_id)
        except Exception:
            logger.exception("quality-bar pass failed for wl %d", watchlist_id)
            demoted = 0
        return {"written": written, "demoted": demoted}

    return asyncio.run(_run())


def _maybe_recompute_rankings(watchlist_id: int) -> None:
    """Fire-and-forget ranking recompute; a dead broker must not fail a hunt."""
    try:
        recompute_rankings_task.delay(watchlist_id)
    except Exception:
        logger.warning("rankings recompute enqueue failed for wl %d", watchlist_id)


@app.task(name="dealbot.worker.tasks.generate_playbook_task")
def generate_playbook_task(watchlist_id: int) -> dict:
    from dealbot.agents.playbook import generate_playbook

    return {"ok": asyncio.run(generate_playbook(watchlist_id))}


@app.task(name="dealbot.worker.tasks.check_price_drops_task")
def check_price_drops_task() -> dict:
    """After a hunt refreshed pool prices: email users whose inspected
    listings dropped below their price-at-inspection. One email per watch."""
    from dealbot.worker.inspections import check_price_drops

    return {"notified": asyncio.run(check_price_drops())}


@app.task(name="dealbot.worker.tasks.auto_inspect_task")
def auto_inspect_task(hunt_id: int, top_n: int = 2) -> dict:
    """Pro perk: pre-inspect the hunt's top new matches so alert emails and
    match rows arrive with Scout's report already cached."""
    from dealbot.worker.inspections import auto_inspect_top_matches

    return {"inspected": asyncio.run(auto_inspect_top_matches(hunt_id, top_n))}


def _maybe_post_hunt_inspections(hunt_id: int, watchlist_id: int) -> None:
    """Fire-and-forget: price-drop sweep always; auto-inspect only for Pro
    owners (checked inside the task to keep this seam broker-cheap)."""
    try:
        check_price_drops_task.delay()
        auto_inspect_task.delay(hunt_id)
    except Exception:
        logger.warning("post-hunt inspection enqueue failed for hunt %d", hunt_id)


def _maybe_generate_playbook(watchlist_id: int) -> None:
    """Fire-and-forget; a dead broker must not fail the calling request."""
    try:
        generate_playbook_task.delay(watchlist_id)
    except Exception:
        logger.warning("playbook enqueue failed for wl %d", watchlist_id)


def _maybe_dispatch_alerts(hunt_id: int) -> None:
    """Seam for tests; production chains alert dispatch off every fruitful hunt.
    A broker blip here must not fail the (already successful, already paid-for)
    hunt — the listings are persisted; alerts surface on the next hunt."""
    from dealbot.worker.alerts import dispatch_alerts

    try:
        dispatch_alerts.delay(hunt_id)
    except Exception:
        logger.warning("alert dispatch enqueue failed for hunt %d", hunt_id, exc_info=True)
