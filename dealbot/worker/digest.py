from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Alert, Deal, User
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

# Swap for real SendGrid/Resend call at deploy time
def _send_email(to: str, subject: str, body: str) -> None:
    logger.info("EMAIL to=%s subject=%r body_length=%d", to, subject, len(body))


def _build_digest(user_email: str, alerts: list[tuple[Alert, Deal]]) -> str:
    lines = [
        f"Hi — here are your DealBot alerts from the last 24 hours:\n",
    ]
    for alert, deal in alerts:
        discount = f" ({deal.real_discount_pct:.0f}% off)" if deal.real_discount_pct else ""
        lines.append(
            f"• {deal.title}{discount}\n"
            f"  Score: {deal.score}/100 | ${deal.sale_price:.2f}\n"
            f"  {deal.url}\n"
        )
    lines.append("\nManage your watchlists at dealbot.app")
    return "\n".join(lines)


async def _send_digests() -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sent = 0
    skipped = 0

    async with get_async_session() as session:
        # Load all users
        users = (await session.execute(select(User))).scalars().all()

        for user in users:
            # Find alerts created in the last 24 hours for this user
            result = await session.execute(
                select(Alert, Deal)
                .join(Deal, Alert.deal_id == Deal.id)
                .where(Alert.user_id == user.id)
                .where(Alert.created_at >= since)
                .order_by(Deal.score.desc())
            )
            rows = result.all()

            if not rows:
                skipped += 1
                continue

            # Pro users get all alerts; free users get digest tier only
            if not user.is_pro:
                rows = [(a, d) for a, d in rows if d.alert_tier == "digest"]

            if not rows:
                skipped += 1
                continue

            body = _build_digest(user.email, rows)
            _send_email(
                to=user.email,
                subject=f"Your DealBot digest — {len(rows)} deal{'s' if len(rows) != 1 else ''}",
                body=body,
            )
            sent += 1

    logger.info("digest: sent=%d skipped=%d", sent, skipped)
    return {"sent": sent, "skipped": skipped}


@app.task(name="dealbot.worker.digest.send_daily_digest")
def send_daily_digest() -> dict:
    return asyncio.run(_send_digests())
