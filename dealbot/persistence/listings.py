"""Persistence for marketplace listings.

Takes `Offer`s from the ExtractorPool and upserts them into the `listings`
table using canonical URLs for dedup. Upsert semantics: on collision, refresh
title/price/condition/image_url + bump `last_seen_at`; preserve `first_seen_at`.

Hunt provenance: when a `hunt_id` is supplied, every upserted listing is linked
to that hunt via `hunt_listings`. `mark_new_for_watchlist` then flags the
listings this watchlist has never surfaced in an earlier hunt — the alert
candidates.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from dealbot.agents.workers.extractor import Offer
from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntListing, Listing
from dealbot.llm.embeddings import embed_texts
from dealbot.persistence.canonicalize import canonicalize_url

logger = logging.getLogger(__name__)


def listing_embed_text(offer: Offer) -> str:
    """Text embedded for semantic search over the pool. Title carries most of
    the signal; condition/marketplace/location disambiguate near-identical
    titles across sites."""
    return f"{offer.title} | {offer.condition} | {offer.marketplace} | {offer.location or ''}"


async def _embeddings_for(offers: list[Offer]) -> list[list[float]]:
    """One batched call. Never raises — a hunt must not lose its listings
    because the embedding service is down."""
    try:
        return await embed_texts([listing_embed_text(o) for o in offers])
    except Exception:
        logger.warning("persist_offers: embedding batch failed", exc_info=True)
        return [[] for _ in offers]


def _as_utc(dt: datetime) -> datetime:
    """Drivers without tz support (sqlite) return stored UTC datetimes naive."""
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


@dataclass
class PersistResult:
    """Identities produced by one persist_offers call."""

    written: int = 0
    listing_ids: list[int] = field(default_factory=list)
    new_global_ids: list[int] = field(default_factory=list)


async def persist_offers(offers: list[Offer], hunt_id: int | None = None) -> PersistResult:
    """Upsert Offers → listings table. Returns the written count plus listing
    identities; rows whose `first_seen_at` was set by this call are globally new.
    """
    if not offers:
        return PersistResult()

    now = datetime.now(timezone.utc)
    embeddings = await _embeddings_for(offers)
    result = PersistResult()
    async with get_async_session() as session:
        for offer, vector in zip(offers, embeddings):
            embedding = vector or None          # [] → NULL
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
                    embedding=embedding,
                    first_seen_at=now,
                    last_seen_at=now,
                )
                .on_conflict_do_update(
                    index_elements=["canonical_url"],
                    set_={
                        "title": offer.title,
                        "price": offer.price,
                        "currency": offer.currency,
                        "location": offer.location,
                        "condition": offer.condition,
                        "embedding": embedding,
                        "last_seen_at": now,
                        # A null image on re-sight means capture didn't run or
                        # failed, not that the listing lost its photo — keep
                        # what we have. Non-null refreshes rotting signed URLs.
                        **({"image_url": offer.image_url} if offer.image_url else {}),
                    },
                )
                .returning(Listing.id, Listing.first_seen_at)
            )
            try:
                row = (await session.execute(stmt)).one()
            except Exception:
                logger.exception(
                    "persist_offers: write failed for %r (canonical=%s)",
                    offer.title, canonical,
                )
                continue
            result.written += 1
            if row.id not in result.listing_ids:
                result.listing_ids.append(row.id)
                # An insert stamps this call's `now`; an update preserves the
                # original first_seen_at — that difference is the novelty test.
                if _as_utc(row.first_seen_at) == now:
                    result.new_global_ids.append(row.id)

        if hunt_id is not None and result.listing_ids:
            link_stmt = (
                pg_insert(HuntListing)
                .values([
                    {"hunt_id": hunt_id, "listing_id": listing_id}
                    for listing_id in result.listing_ids
                ])
                .on_conflict_do_nothing()
            )
            await session.execute(link_stmt)

        await session.commit()
    return result


async def mark_new_for_watchlist(hunt_id: int) -> list[int]:
    """Set was_new_for_watchlist=true on this hunt's hunt_listings rows whose
    listing has never appeared in an EARLIER hunt of the same watchlist.
    Returns the marked listing_ids (sorted). Idempotent."""
    async with get_async_session() as session:
        hunt = await session.get(Hunt, hunt_id)
        if hunt is None:
            return []
        prior = (
            select(HuntListing.listing_id)
            .join(Hunt, HuntListing.hunt_id == Hunt.id)
            .where(
                Hunt.watchlist_id == hunt.watchlist_id,
                Hunt.id != hunt_id,
                Hunt.started_at < hunt.started_at,
            )
        )
        rows = (
            await session.execute(
                select(HuntListing).where(
                    HuntListing.hunt_id == hunt_id,
                    HuntListing.listing_id.not_in(prior),
                )
            )
        ).scalars().all()
        for row in rows:
            row.was_new_for_watchlist = True
        await session.commit()
        return sorted(r.listing_id for r in rows)
