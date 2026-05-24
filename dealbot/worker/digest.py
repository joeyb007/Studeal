from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, text

from dealbot.db.database import get_async_session
from dealbot.db.models import Deal, User
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

_RESEND_API_URL = "https://api.resend.com/emails"
_FROM_ADDRESS = os.environ.get("RESEND_FROM", "Studeal <alerts@studeal.site>")
# pgvector cosine distance threshold — anything <= this is "close enough"
_SIMILARITY_THRESHOLD = 0.45
# Cap deals-per-user-per-digest so emails stay skim-able
_MAX_DEALS_PER_DIGEST = 20


async def _send_email(to: str, subject: str, body: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "")
    if not api_key:
        logger.warning("RESEND_API_KEY not set — skipping email to %s", to)
        return

    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.post(
                _RESEND_API_URL,
                json={"from": _FROM_ADDRESS, "to": [to], "subject": subject, "text": body},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            logger.info("digest: sent email to %s (id=%s)", to, resp.json().get("id"))
        except httpx.HTTPStatusError as exc:
            logger.error("digest: Resend error %d for %s — %s", exc.response.status_code, to, exc.response.text)
        except Exception:
            logger.exception("digest: failed to send email to %s", to)


def _build_digest(user_email: str, matches: list[tuple[str, Deal]]) -> str:
    lines = ["Here are your Studeal matches from the last 24 hours:\n"]
    for watchlist_name, deal in matches:
        discount = f" ({deal.real_discount_pct:.0f}% off)" if deal.real_discount_pct else ""
        lines.append(
            f"• [{watchlist_name}] {deal.title}{discount}\n"
            f"  ${deal.sale_price:.2f}\n"
            f"  {deal.url}\n"
        )
    lines.append("\nManage your agents at studeal.site")
    return "\n".join(lines)


async def _matched_deals_for_user(session, user: User, since: datetime) -> list[tuple[str, Deal]]:
    """
    Find fresh legitimate deals matching each of this user's agents.

    Uses pgvector cosine distance against each Watchlist.intent_embedding.
    Filters to legitimate=true. Returns matches ordered by similarity, capped
    at _MAX_DEALS_PER_DIGEST per user.
    """
    result = await session.execute(
        text("""
            SELECT DISTINCT ON (d.id)
                d.*,
                w.name AS watchlist_name,
                d.embedding <=> w.intent_embedding AS distance
            FROM deals d
            JOIN watchlists w
              ON (d.embedding <=> w.intent_embedding) <= :threshold
            WHERE w.user_id = :user_id
              AND d.first_seen_at >= :since
              AND d.embedding IS NOT NULL
              AND w.intent_embedding IS NOT NULL
              AND d.legitimate = true
              AND (w.expires_at IS NULL OR w.expires_at > now())
            ORDER BY d.id, distance ASC
        """),
        {
            "user_id": user.id,
            "since": since,
            "threshold": _SIMILARITY_THRESHOLD,
        },
    )
    rows = result.mappings().all()

    # Order across watchlists by best (smallest) cosine distance, then cap
    sorted_rows = sorted(rows, key=lambda r: r["distance"])
    matches: list[tuple[str, Deal]] = []
    for row in sorted_rows[:_MAX_DEALS_PER_DIGEST]:
        deal_fields = {k: v for k, v in row.items() if k not in ("watchlist_name", "distance")}
        matches.append((row["watchlist_name"], Deal(**deal_fields)))
    return matches


async def _send_digests() -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sent = 0
    skipped = 0

    async with get_async_session() as session:
        all_users = (await session.execute(select(User).where(User.is_pro == True))).scalars().all()  # noqa: E712
        users = [u for u in all_users if u.is_pro]

        for user in users:
            matches = await _matched_deals_for_user(session, user, since)
            if not matches:
                skipped += 1
                continue

            body = _build_digest(user.email, matches)
            await _send_email(
                to=user.email,
                subject=f"Your Studeal digest — {len(matches)} deal{'s' if len(matches) != 1 else ''}",
                body=body,
            )
            sent += 1

    logger.info("digest: sent=%d skipped=%d", sent, skipped)
    return {"sent": sent, "skipped": skipped}


@app.task(name="dealbot.worker.digest.send_daily_digest")
def send_daily_digest() -> dict:
    return asyncio.run(_send_digests())
