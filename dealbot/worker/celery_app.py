from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    "dealbot",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["dealbot.worker.tasks"],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    # Run the scraper every 15 minutes
    beat_schedule={
        "scrape-slickdeals": {
            "task": "dealbot.worker.tasks.scrape_slickdeals",
            "schedule": 900,  # seconds
        },
    },
)
