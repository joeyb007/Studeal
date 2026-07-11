"""Persistence for marketplace listings.

Takes `Offer`s from the ExtractorPool and upserts them into the `listings`
table using canonical URLs for dedup. Upsert semantics: on collision, refresh
title/price/condition/image_url + bump `last_seen_at`; preserve `first_seen_at`.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.agents.workers.extractor import Offer
from dealbot.db.database import get_async_session
from dealbot.db.models import Listing
from dealbot.persistence.canonicalize import canonicalize_url

logger = logging.getLogger(__name__)


async def persist_offers(offers: list[Offer]) -> int:
    """Upsert Offers → listings table. Returns count of successful writes."""
    if not offers:
        return 0

    now = datetime.now(timezone.utc)
    written = 0
    async with get_async_session() as session:
        for offer in offers:
            canonical = canonicalize_url(offer.url, offer.marketplace)
            stmt = (
                pg_insert(Listing)
                .values(
                    canonical_url=canonical,
                    raw_url=offer.url,
                    marketplace=offer.marketplace,
                    title=offer.title,
                    price=offer.price,
                    currency=offer.currency,
                    image_url=offer.image_url,
                    location=offer.location,
                    posted_at_raw=offer.posted_at_raw,
                    condition=offer.condition,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["canonical_url"],
                    set_={
                        "title": offer.title,
                        "price": offer.price,
                        "currency": offer.currency,
                        "image_url": offer.image_url,
                        "location": offer.location,
                        "condition": offer.condition,
                        "last_seen_at": now,
                    },
                )
            )
            try:
                await session.execute(stmt)
                written += 1
            except Exception:
                logger.exception(
                    "persist_offers: write failed for %r (canonical=%s)",
                    offer.title, canonical,
                )
        await session.commit()
    return written
