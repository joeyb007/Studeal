from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sentry_sdk
from celery import Celery
from celery.schedules import crontab
from dotenv import load_dotenv
from sentry_sdk.integrations.celery import CeleryIntegration
from sentry_sdk.integrations.logging import LoggingIntegration

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

_SENTRY_DSN = os.environ.get("SENTRY_DSN", "")
if _SENTRY_DSN:
    sentry_sdk.init(
        dsn=_SENTRY_DSN,
        traces_sample_rate=0.1,
        send_default_pii=False,
        integrations=[
            CeleryIntegration(),
            LoggingIntegration(
                level=logging.WARNING,
                event_level=logging.ERROR,
            ),
        ],
    )

logger = logging.getLogger(__name__)

app = Celery(
    "dealbot",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "dealbot.worker.tasks",
        "dealbot.worker.digest",
        "dealbot.worker.scheduler",
        "dealbot.worker.alerts",
        "dealbot.worker.celery_app",
    ],
)

app.conf.update(
    # Redis priority queues: first hunts (new users) outrank scheduled
    # re-hunts — a cron refresh can be minutes late, a first impression can't.
    broker_transport_options={"queue_order_strategy": "priority"},
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        # Every 5 min — enqueue hunts for watchlists whose cadence has elapsed
        "schedule-due-hunts": {
            "task": "dealbot.worker.scheduler.schedule_due_hunts",
            "schedule": crontab(minute="*/5"),
        },
        # 08:00 UTC — email digest to pro users with yesterday's matches
        "send-daily-digest": {
            "task": "dealbot.worker.digest.send_daily_digest",
            "schedule": crontab(hour=8, minute=0),
        },
        # 05:00 UTC — clean up deals older than 3 days before the new seed run
        "cleanup-old-deals": {
            "task": "dealbot.worker.celery_app.cleanup_old_deals",
            "schedule": crontab(hour=5, minute=0),
        },
        # 05:10 UTC — delete watchlists past their expires_at
        "cleanup-stale-watchlists": {
            "task": "dealbot.worker.celery_app.cleanup_stale_watchlists",
            "schedule": crontab(hour=5, minute=10),
        },
        # 05:20 UTC — hard-delete listings unseen for LISTING_PURGE_DAYS.
        # Reads already stopped serving them at LISTING_STALE_DAYS; this is
        # capacity management, deliberately far behind so price history
        # survives for the future price-intelligence layer.
        "cleanup-old-listings": {
            "task": "dealbot.worker.celery_app.cleanup_old_listings",
            "schedule": crontab(hour=5, minute=20),
        },
    },
)


@app.task(name="dealbot.worker.celery_app.cleanup_old_deals")
def cleanup_old_deals() -> dict:
    """Delete deal rows with scraped_at older than 3 days. Alerts are kept (cascade=False on deal FK)."""
    return asyncio.run(_run_cleanup())


async def _run_cleanup() -> dict:
    from sqlalchemy import delete

    from dealbot.db.database import get_async_session
    from dealbot.db.models import Deal

    cutoff = datetime.now(timezone.utc) - timedelta(days=3)
    async with get_async_session() as session:
        result = await session.execute(
            delete(Deal).where(Deal.scraped_at < cutoff).returning(Deal.id)
        )
        deleted = len(result.fetchall())
        await session.commit()

    logger.info("cleanup_old_deals: deleted %d deal(s) older than 3 days", deleted)
    return {"deleted": deleted}


@app.task(name="dealbot.worker.celery_app.cleanup_old_listings")
def cleanup_old_listings() -> dict:
    """Delete listings unseen for LISTING_PURGE_DAYS. FK cascade removes their
    listing_alerts and hunt_listings rows — accepted (90-day-old history)."""
    return asyncio.run(_run_listing_purge())


async def _run_listing_purge() -> dict:
    from sqlalchemy import delete

    from dealbot.db.database import get_async_session
    from dealbot.db.models import Listing
    from dealbot.lifecycle import purge_cutoff

    async with get_async_session() as session:
        result = await session.execute(
            delete(Listing)
            .where(Listing.last_seen_at < purge_cutoff())
            .returning(Listing.id)
        )
        deleted = len(result.fetchall())
        await session.commit()

    logger.info("cleanup_old_listings: deleted %d listing(s)", deleted)
    return {"deleted": deleted}


@app.task(name="dealbot.worker.celery_app.cleanup_stale_watchlists")
def cleanup_stale_watchlists() -> dict:
    """Delete watchlists whose expires_at has passed."""
    return asyncio.run(_run_watchlist_cleanup())


async def _run_watchlist_cleanup() -> dict:
    from sqlalchemy import delete

    from dealbot.db.database import get_async_session
    from dealbot.db.models import Watchlist

    now = datetime.now(timezone.utc)
    async with get_async_session() as session:
        result = await session.execute(
            delete(Watchlist)
            .where(Watchlist.expires_at.isnot(None))
            .where(Watchlist.expires_at < now)
            .returning(Watchlist.id)
        )
        deleted = len(result.fetchall())
        await session.commit()

    logger.info("cleanup_stale_watchlists: deleted %d expired watchlist(s)", deleted)
    return {"deleted": deleted}
