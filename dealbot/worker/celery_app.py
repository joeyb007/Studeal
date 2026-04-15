from __future__ import annotations

import os

from celery import Celery

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")

app = Celery(
    "dealbot",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["dealbot.worker.tasks", "dealbot.worker.digest"],
)

app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    beat_schedule={
        "hunt-deals": {
            "task": "dealbot.worker.tasks.hunt_deals",
            "schedule": 86400,  # once daily
        },
        "send-daily-digest": {
            "task": "dealbot.worker.digest.send_daily_digest",
            "schedule": 86400,  # once daily
        },
    },
)
