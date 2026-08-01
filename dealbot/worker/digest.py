from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, User
from dealbot.lifecycle import is_internal_user
from dealbot.notifications.email import send_email
from dealbot.worker.celery_app import app

logger = logging.getLogger(__name__)

# pgvector cosine distance threshold — anything <= this is "close enough"
_SIMILARITY_THRESHOLD = 0.60  # distance; sim>=0.40, aligned to the gate bar in Titan space
# Cap listings-per-user-per-digest so emails stay skim-able
_MAX_LISTINGS_PER_DIGEST = 20


def _build_digest(user_email: str, matches: list[tuple[str, Listing]]) -> str:
    """Pool listings have no list price to discount from, so the old
    "(20% off)" suffix and the `legitimate` filter both retire with `deals`."""
    lines = ["Here are your Studeal matches from the last 24 hours:\n"]
    for watchlist_name, listing in matches:
        lines.append(
            f"• [{watchlist_name}] {listing.title}\n"
            f"  ${listing.price:.2f} {listing.currency} — {listing.marketplace}\n"
            f"  {listing.raw_url}\n"
        )
    lines.append("\nManage your agents at studeal.site")
    return "\n".join(lines)


async def _matched_listings_for_user(
    session, user: User, since: datetime,
) -> list[tuple[str, Listing]]:
    """
    Find fresh pool listings matching each of this user's agents.

    pgvector cosine distance against each Watchlist.intent_embedding — which,
    since Workstream C, encodes the whole elicited context rather than a bare
    product query. Returns matches ordered by similarity, capped at
    _MAX_LISTINGS_PER_DIGEST per user.
    """
    result = await session.execute(
        text("""
            SELECT DISTINCT ON (l.id)
                l.*,
                w.name AS watchlist_name,
                l.embedding <=> w.intent_embedding AS distance
            FROM listings l
            JOIN watchlists w
              ON (l.embedding <=> w.intent_embedding) <= :threshold
            WHERE w.user_id = :user_id
              AND l.first_seen_at >= :since
              AND l.embedding IS NOT NULL
              AND w.intent_embedding IS NOT NULL
              AND (w.expires_at IS NULL OR w.expires_at > now())
            ORDER BY l.id, distance ASC
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
    matches: list[tuple[str, Listing]] = []
    for row in sorted_rows[:_MAX_LISTINGS_PER_DIGEST]:
        fields = {k: v for k, v in row.items() if k not in ("watchlist_name", "distance")}
        matches.append((row["watchlist_name"], Listing(**fields)))
    return matches


async def _send_digests() -> dict:
    since = datetime.now(timezone.utc) - timedelta(hours=24)
    sent = 0
    skipped = 0

    async with get_async_session() as session:
        all_users = (await session.execute(select(User).where(User.is_pro == True))).scalars().all()  # noqa: E712
        users = [u for u in all_users if u.is_pro and not is_internal_user(u.email)]

        for user in users:
            matches = await _matched_listings_for_user(session, user, since)
            if not matches:
                skipped += 1
                continue

            body = _build_digest(user.email, matches)
            await send_email(
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
