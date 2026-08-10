"""Post-hunt inspection work: price-drop watches and the Pro auto-inspect.

Both run fire-and-forget after every hunt. Both are best-effort: a failure
here never touches the hunt that spawned it.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import (
    Hunt,
    InspectionWatch,
    Listing,
    ListingAlert,
    ListingInspection,
    User,
    Watchlist,
)
from dealbot.lifecycle import is_internal_user
from dealbot.notifications.email import send_email

logger = logging.getLogger(__name__)

# Top-5 pre-reads power the agent card's ✦ teasers (redesign spec 2026-08-09):
# system-initiated, cached per listing, never counted against the free
# allowance. Cost tracks NEW picks only (cache-by-listing absorbs repeats).
AUTO_INSPECT_TOP_N = 5


def build_price_drop_email(listing: Listing, old_price: float) -> tuple[str, str]:
    """→ (subject, body). The friend-remembered-you-asked email."""
    subject = f"Studeal: the {listing.title[:60]} you asked about dropped to ${listing.price:.0f}"
    body = "\n".join([
        f"You sent this one to Scout at ${old_price:.0f}. It just dropped to "
        f"${listing.price:.0f} ({listing.marketplace}).",
        "",
        listing.title,
        listing.raw_url,
        "",
        "Worth another look before someone else grabs it.",
        "",
        "Manage your agents at studeal.site",
    ])
    return subject, body


async def check_price_drops() -> int:
    """Email every watch whose listing now sits below its price-at-inspection.
    One notification per watch, ever (notified_at guards)."""
    notified = 0
    async with get_async_session() as session:
        rows = (await session.execute(
            select(InspectionWatch, Listing, User)
            .join(Listing, Listing.id == InspectionWatch.listing_id)
            .join(User, User.id == InspectionWatch.user_id)
            .where(InspectionWatch.notified_at.is_(None))
            .where(Listing.sold_at.is_(None))
            .where(Listing.price < InspectionWatch.price_at_inspection)
        )).all()

        for watch, listing, user in rows:
            if is_internal_user(user.email):
                watch.notified_at = datetime.now(timezone.utc)
                continue
            subject, body = build_price_drop_email(listing, watch.price_at_inspection)
            try:
                sent = await send_email(user.email, subject, body)
            except Exception:
                logger.exception("price-drop email failed (user %d)", user.id)
                continue
            if sent:
                watch.notified_at = datetime.now(timezone.utc)
                notified += 1
        await session.commit()
    return notified


async def auto_inspect_top_matches(hunt_id: int, top_n: int = AUTO_INSPECT_TOP_N) -> int:
    """Pre-run Tier A on the hunt's best new matches so every agent card's
    top picks open with Scout's read already cached (all users; the card's
    teasers depend on it)."""
    from dealbot.agents.inspector import get_or_create_inspection

    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is None:
            return 0
        watchlist = await session.get(Watchlist, hunt.watchlist_id)
        if watchlist is None:
            return 0
        owner = await session.get(User, watchlist.user_id)
        if owner is None:
            return 0

        candidates = (await session.execute(
            select(ListingAlert.listing_id)
            .outerjoin(ListingInspection,
                       ListingInspection.listing_id == ListingAlert.listing_id)
            .where(ListingAlert.hunt_id == hunt_id)
            .where(ListingInspection.listing_id.is_(None))  # not yet inspected
            .order_by(ListingAlert.score.desc())
            .limit(top_n)
        )).scalars().all()

    inspected = 0
    for listing_id in candidates:
        try:
            result = await get_or_create_inspection(listing_id)
            if result["status"] in ("ok", "listing_gone"):
                inspected += 1
        except Exception:
            logger.exception("auto-inspect failed for listing %d", listing_id)
    return inspected
