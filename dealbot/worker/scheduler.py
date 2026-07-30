"""schedule_due_hunts — Celery beat task that turns watchlists into a fleet.

Every tick (5 min): find watchlists whose hunt cadence has elapsed
(tier-based: free daily, pro hourly; per-watchlist override wins), and enqueue
`research_for_agent` for as many as the FleetGovernor's capacity allows,
staggered by the governor's start gap. `last_hunt_at` is stamped optimistically
at enqueue time so the next tick can't double-enqueue; the worker overwrites
it with the true hunt start.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, User, Watchlist
from dealbot.worker.celery_app import app
from dealbot.worker.governor import FleetGovernor, build_governor

logger = logging.getLogger(__name__)

FREE_HUNT_CADENCE_MIN = int(os.environ.get("FREE_HUNT_CADENCE_MIN", "1440"))
PRO_HUNT_CADENCE_MIN = int(os.environ.get("PRO_HUNT_CADENCE_MIN", "60"))


def cadence_minutes(watchlist: Watchlist, user: User) -> int:
    if watchlist.hunt_frequency_minutes is not None:
        return watchlist.hunt_frequency_minutes
    return PRO_HUNT_CADENCE_MIN if user.is_pro else FREE_HUNT_CADENCE_MIN


def _as_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def is_due(watchlist: Watchlist, user: User, now: datetime) -> bool:
    if not watchlist.hunting_enabled or not watchlist.context:
        return False
    if watchlist.expires_at is not None and _as_utc(watchlist.expires_at) <= now:
        return False
    if watchlist.last_hunt_at is None:
        return True
    elapsed = now - _as_utc(watchlist.last_hunt_at)
    return elapsed >= timedelta(minutes=cadence_minutes(watchlist, user))


async def select_due_watchlists(session, now: datetime) -> list[Watchlist]:
    """All due watchlists, never-hunted first, then stalest first."""
    rows = (
        await session.execute(
            select(Watchlist)
            .options(selectinload(Watchlist.user))
            .where(Watchlist.hunting_enabled.is_(True))
            .where(Watchlist.context.is_not(None))
        )
    ).scalars().all()
    due = [wl for wl in rows if is_due(wl, wl.user, now)]
    sentinel = datetime.min.replace(tzinfo=timezone.utc)
    due.sort(key=lambda wl: _as_utc(wl.last_hunt_at) if wl.last_hunt_at else sentinel)
    return due


async def _schedule(
    now: datetime | None = None,
    *,
    governor: FleetGovernor | None = None,
    enqueue: Callable[[int, int], Awaitable[None]] | None = None,
) -> dict:
    """Injectable seams (`governor`, `enqueue`) exist for tests; production
    uses the real governor and research_for_agent.apply_async."""
    now = now or datetime.now(timezone.utc)
    governor = governor or build_governor()
    if enqueue is None:
        from dealbot.worker.tasks import research_for_agent

        async def enqueue(wl_id: int, countdown: int) -> None:
            research_for_agent.apply_async(args=[wl_id], countdown=countdown)

    # Tick mutex: a tick delayed behind a long task can otherwise run
    # concurrently with the next one and double-enqueue the same due set.
    try:
        locked = await governor.acquire_tick_lock()
    except Exception:
        logger.warning("tick lock unavailable — proceeding unlocked", exc_info=True)
        locked = True
    if not locked:
        logger.info("schedule_due_hunts: another tick holds the lock — skipping")
        return {"enqueued": 0, "due": 0, "skipped_capacity": 0}

    enqueued = 0
    skipped_capacity = 0
    try:
        async with get_async_session() as session:
            reaped = await _reap_stale_hunts(session, now)
            due = await select_due_watchlists(session, now)
            for watchlist in due:
                if not await governor.has_capacity(pending=enqueued):
                    skipped_capacity = len(due) - enqueued
                    break
                await enqueue(watchlist.id, enqueued * governor.min_start_gap_s)
                watchlist.last_hunt_at = now
                enqueued += 1
            await session.commit()
    finally:
        try:
            await governor.release_tick_lock()
        except Exception:
            logger.warning("tick lock release failed (TTL will expire it)", exc_info=True)

    if due or reaped:
        logger.info(
            "schedule_due_hunts: due=%d enqueued=%d skipped_capacity=%d reaped=%d",
            len(due), enqueued, skipped_capacity, reaped,
        )
    return {"enqueued": enqueued, "due": len(due), "skipped_capacity": skipped_capacity}


async def _reap_stale_hunts(session, now: datetime) -> int:
    """Fail out hunts stuck in `running` past the governor's staleness horizon —
    a SIGKILLed worker leaves the row behind, and Mission Control renders DB
    state on load, so stale `running` hunts are user-visible forever otherwise."""
    horizon = now - timedelta(seconds=FleetGovernor.STALE_S)
    stale = (
        await session.execute(
            select(Hunt).where(Hunt.status == "running", Hunt.started_at < horizon)
        )
    ).scalars().all()
    for hunt in stale:
        hunt.status = "failed"
        hunt.finished_at = now
        hunt.error = "reaped: no completion within staleness horizon (worker lost)"
    return len(stale)


@app.task(name="dealbot.worker.scheduler.schedule_due_hunts")
def schedule_due_hunts() -> dict:
    return asyncio.run(_schedule())
