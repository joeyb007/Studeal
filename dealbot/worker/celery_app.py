from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from celery import Celery
from celery.schedules import crontab

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

logger = logging.getLogger(__name__)

app = Celery(
    "dealbot",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=[
        "dealbot.worker.tasks",
        "dealbot.worker.digest",
        "dealbot.worker.seed",
        "dealbot.worker.celery_app",
    ],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        # 06:00 UTC — seed editorial catalog before users wake up
        "seed-deals": {
            "task": "dealbot.worker.seed.seed_deals",
            "schedule": crontab(hour=6, minute=0),
        },
        # 07:00 UTC — hunt watchlist keywords on fresh seed data
        "hunt-deals": {
            "task": "dealbot.worker.tasks.hunt_deals",
            "schedule": crontab(hour=7, minute=0),
        },
        # Digest disabled for v1 — re-engagement handled via watchlist page
        # "send-daily-digest": {
        #     "task": "dealbot.worker.digest.send_daily_digest",
        #     "schedule": crontab(hour=8, minute=0),
        # },
        # 05:00 UTC — clean up deals older than 3 days before the new seed run
        "cleanup-old-deals": {
            "task": "dealbot.worker.celery_app.cleanup_old_deals",
            "schedule": crontab(hour=5, minute=0),
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
