"""Sufficiency gate — how much browsing does this refresh need?

    sufficient? = count(pool ∩ fresh ∩ novel-to-W ∩ cosine ≥ τ) ≥ k

Measured in outcomes (listings matching the intent vector), not proxies
(query-string similarity), so the failure modes degrade into extra hunting,
never a silently frozen pool: degenerate queries thin the pool → insufficiency
trips more → MORE hunting. A too-loose τ self-corrects — junk matches are
consumed as hunt_listings rows, the junk supply exhausts, insufficiency trips.

τ=0.50 / k=10 RE-MEASURED in Titan MULTIMODAL space (2026-08-11, fused
photo+text listing vectors, intent docs with the physical brief): same-
category distances run 0.22-0.49 (similarity 0.51-0.78); cross-category
starts at distance ~0.51 (similarity ≤0.49). The bands TOUCH near 0.51 —
title-blind listings with weak photos straddle it and fail toward hunting,
which is the safe direction. τ is coupled to BOTH the embedding backend and
compose_intent_document — changing either expires the number (Titan text-V2
measurement was τ=0.40; OpenAI-space was τ=0.50). See spec §8b.
"""

from __future__ import annotations

import logging
import os

from sqlalchemy import select

from dealbot.db.database import get_async_session
from dealbot.db.models import Hunt, HuntListing, Listing, Watchlist
from dealbot.lifecycle import stale_cutoff
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

GATE_SIMILARITY_TAU = float(os.environ.get("GATE_SIMILARITY_TAU", "0.50"))
GATE_SUFFICIENCY_K = int(os.environ.get("GATE_SUFFICIENCY_K", "10"))


async def pool_candidates(watchlist_id: int, *, limit: int = 50) -> list[Listing]:
    """Fresh, novel-to-this-watchlist pool listings within τ of its intent.

    Hard constraints (budget, condition) are SQL filters here — these rows
    become alert candidates, and the ranker must never see what it cannot
    have. Returns [] when the watchlist has no intent vector: no vector, no
    gate — the caller falls through to a full hunt.

    Embed lag: hunts persist with NULL vectors and a consumer task fills them
    minutes later, so this count briefly undercounts a just-finished hunt.
    That failure direction is safe — undercounting triggers a hunt, never a
    stale cache serve.
    """
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.intent_embedding is None:
            return []
        context = (
            WatchlistContext.model_validate_json(watchlist.context)
            if watchlist.context else None
        )

        # Novelty: anything ever linked to one of this watchlist's hunts is
        # consumed. This is what makes dedup emergent — served candidates
        # stop counting, so tomorrow needs new arrivals or a real hunt.
        seen = (
            select(HuntListing.listing_id)
            .join(Hunt, HuntListing.hunt_id == Hunt.id)
            .where(Hunt.watchlist_id == watchlist_id)
        )

        distance_cutoff = 1.0 - GATE_SIMILARITY_TAU
        stmt = (
            select(Listing)
            .where(Listing.embedding.isnot(None))
            .where(Listing.last_seen_at >= stale_cutoff())
            .where(Listing.sold_at.is_(None))
            .where(Listing.id.not_in(seen))
            .where(
                Listing.embedding.cosine_distance(watchlist.intent_embedding)
                < distance_cutoff
            )
            .order_by(Listing.embedding.cosine_distance(watchlist.intent_embedding))
            .limit(limit)
        )
        if context is not None:
            if context.max_budget is not None:
                stmt = stmt.where(Listing.price <= context.max_budget)
            if context.condition:
                stmt = stmt.where(Listing.condition.in_(context.condition))

        return list((await session.execute(stmt)).scalars().all())


async def link_pool_candidates(hunt_id: int, listing_ids: list[int]) -> int:
    """Attach pool-served listings to a hunt as source="pool" rows.

    Skips ids already linked (composite PK; a browsed find can also be a pool
    match — browsing wins the provenance tie). Returns rows inserted.
    """
    if not listing_ids:
        return 0
    async with get_async_session() as session:
        existing = {
            lid for (lid,) in (await session.execute(
                select(HuntListing.listing_id).where(HuntListing.hunt_id == hunt_id)
            )).all()
        }
        inserted = 0
        for listing_id in listing_ids:
            if listing_id in existing:
                continue
            session.add(HuntListing(
                hunt_id=hunt_id, listing_id=listing_id, source="pool",
            ))
            inserted += 1
        await session.commit()
        return inserted
