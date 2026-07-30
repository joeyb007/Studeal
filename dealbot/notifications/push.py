"""Web push sender (VAPID via pywebpush).

Sends to every subscription a user has registered. Push services answer
404/410 for expired subscriptions — those rows are deleted on sight, so the
table is self-cleaning. pywebpush is sync (requests under the hood); sends
run in a thread to keep the event loop unblocked.
"""

from __future__ import annotations

import asyncio
import logging
import os

from pydantic import BaseModel
from pywebpush import WebPushException, webpush
from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import PushSubscription

logger = logging.getLogger(__name__)


class PushPayload(BaseModel):
    title: str
    body: str
    url: str


async def send_push_to_user(user_id: int, payload: PushPayload) -> int:
    """Push `payload` to all of the user's subscriptions. Returns successful
    send count. Missing VAPID config → 0 (logged, never raised)."""
    private_key = os.environ.get("VAPID_PRIVATE_KEY", "")
    subject = os.environ.get("VAPID_SUBJECT", "")
    if not private_key or not subject:
        logger.warning("send_push_to_user: VAPID env vars not set — skipping push")
        return 0

    async with get_async_session() as session:
        subscriptions = (
            await session.execute(
                select(PushSubscription).where(PushSubscription.user_id == user_id)
            )
        ).scalars().all()

        sent = 0
        for sub in subscriptions:
            info = {
                "endpoint": sub.endpoint,
                "keys": {"p256dh": sub.p256dh, "auth": sub.auth},
            }
            try:
                await asyncio.to_thread(
                    webpush,
                    subscription_info=info,
                    data=payload.model_dump_json(),
                    vapid_private_key=private_key,
                    vapid_claims={"sub": subject},
                )
                sent += 1
            except WebPushException as exc:
                status = getattr(exc.response, "status_code", None)
                if status in (404, 410):
                    logger.info(
                        "send_push_to_user: subscription expired (%s) — deleting",
                        sub.endpoint,
                    )
                    await session.delete(sub)
                else:
                    logger.warning(
                        "send_push_to_user: push failed for %s: %s", sub.endpoint, exc,
                    )
            except Exception:
                logger.exception("send_push_to_user: unexpected failure for %s", sub.endpoint)
        await session.commit()
    return sent
