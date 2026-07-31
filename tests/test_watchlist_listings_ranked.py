"""The watchlist read path: dense retrieval over the shared pool, then ranking.

This is where the pool thesis pays off — a watchlist can surface listings that
ANOTHER user's agent found, which the provenance-scoped alert path can never
reach. The invariant that must hold regardless: hard constraints are SQL
filters, so no ranker verdict can surface an over-budget listing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Listing, Watchlist
from dealbot.schemas import WatchlistContext

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def rig(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr(
        "dealbot.api.routes.watchlists.get_async_session", _test_session,
    )
    return authed_client, factory, monkeypatch


async def _seed(factory, *, prices: list[float], context: WatchlistContext) -> int:
    """One watchlist (no intent_embedding — sqlite has no pgvector) plus one
    pool listing per price. Returns the watchlist id."""
    async with factory() as s:
        watchlist = Watchlist(
            user_id=1, name="Chair", context=context.model_dump_json(),
        )
        s.add(watchlist)
        await s.flush()
        for i, price in enumerate(prices):
            s.add(Listing(
                canonical_url=f"c{i}", raw_url=f"https://m.test/{i}",
                marketplace="kijiji", title=f"Aeron {i}", price=price,
                currency="CAD", condition="used",
                first_seen_at=NOW - timedelta(hours=1),
                last_seen_at=NOW - timedelta(minutes=i),
            ))
        wl_id = watchlist.id
        await s.commit()
        return wl_id


@pytest.mark.asyncio
async def test_listings_carry_ranker_reasons(rig):
    """The read path and the alert path produce the same shape — one rank()
    serves both."""
    client, factory, mp = rig
    from dealbot.recsys.ranker import RankedListing

    wl_id = await _seed(
        factory, prices=[100.0, 200.0],
        context=WatchlistContext(product_query="aeron chair"),
    )

    async def _fake_rank(spec, candidates, *, llm=None, top_n=20):
        return [
            RankedListing(listing=c, score=0.7, reason="Matches your setup.")
            for c in candidates[:top_n]
        ]

    mp.setattr("dealbot.api.routes.watchlists.rank", _fake_rank)
    resp = client.get(f"/watchlists/{wl_id}/listings")

    assert resp.status_code == 200, resp.text
    rows = resp.json()["listings"]
    assert rows, "seeded listings must come back"
    assert all(r["reason"] == "Matches your setup." for r in rows)
    assert all(r["relevance_score"] == 0.7 for r in rows)


@pytest.mark.asyncio
async def test_budget_ceiling_is_enforced_in_sql_not_by_the_ranker(rig):
    """A ranker that loves an over-budget listing must not be able to surface
    it — the ceiling is applied before the ranker ever sees the row."""
    client, factory, mp = rig
    from dealbot.recsys.ranker import RankedListing

    wl_id = await _seed(
        factory, prices=[50.0, 5000.0],
        context=WatchlistContext(product_query="aeron chair", max_budget=100.0),
    )

    seen: dict = {}

    async def _rank_everything(spec, candidates, *, llm=None, top_n=20):
        seen["prices"] = [c.price for c in candidates]
        return [RankedListing(listing=c, score=1.0, reason="great") for c in candidates]

    mp.setattr("dealbot.api.routes.watchlists.rank", _rank_everything)
    rows = client.get(f"/watchlists/{wl_id}/listings").json()["listings"]

    assert seen["prices"] == [50.0], "the ranker never sees over-budget rows"
    assert all(r["price"] <= 100.0 * 1.2 for r in rows)


@pytest.mark.asyncio
async def test_condition_is_a_sql_filter(rig):
    client, factory, mp = rig
    from dealbot.recsys.ranker import RankedListing

    wl_id = await _seed(
        factory, prices=[100.0],
        context=WatchlistContext(product_query="aeron chair", condition=["new"]),
    )

    async def _rank(spec, candidates, *, llm=None, top_n=20):
        return [RankedListing(listing=c, score=0.5, reason="") for c in candidates]

    mp.setattr("dealbot.api.routes.watchlists.rank", _rank)
    rows = client.get(f"/watchlists/{wl_id}/listings").json()["listings"]
    assert rows == [], "the seeded listing is 'used'; condition=['new'] excludes it"


@pytest.mark.asyncio
async def test_no_candidates_returns_empty_without_calling_the_ranker(rig):
    client, factory, mp = rig

    wl_id = await _seed(
        factory, prices=[],
        context=WatchlistContext(product_query="aeron chair"),
    )

    called = {"n": 0}

    async def _rank(spec, candidates, *, llm=None, top_n=20):
        called["n"] += 1
        return []

    mp.setattr("dealbot.api.routes.watchlists.rank", _rank)
    data = client.get(f"/watchlists/{wl_id}/listings").json()

    assert data["listings"] == []
    assert data["total_candidates"] == 0
    assert called["n"] == 0, "no candidates → no LLM spend"


@pytest.mark.asyncio
async def test_stale_pool_listings_never_reach_the_ranker(rig):
    """Cosine proximity must not resurrect a listing the fleet stopped seeing."""
    from dealbot.lifecycle import LISTING_STALE_DAYS
    from dealbot.recsys.ranker import RankedListing

    client, factory, mp = rig
    wl_id = await _seed(
        factory, prices=[100.0],
        context=WatchlistContext(product_query="aeron chair"),
    )
    async with factory() as s:
        s.add(Listing(
            canonical_url="stale", raw_url="https://m.test/stale",
            marketplace="kijiji", title="Sold Weeks Ago", price=90.0,
            currency="CAD", condition="used",
            first_seen_at=NOW - timedelta(days=LISTING_STALE_DAYS + 5),
            last_seen_at=NOW - timedelta(days=LISTING_STALE_DAYS + 5),
        ))
        await s.commit()

    seen: dict = {}

    async def _rank(spec, candidates, *, llm=None, top_n=20):
        seen["titles"] = [c.title for c in candidates]
        return [RankedListing(listing=c, score=0.5, reason="") for c in candidates]

    mp.setattr("dealbot.api.routes.watchlists.rank", _rank)
    client.get(f"/watchlists/{wl_id}/listings")
    assert "Sold Weeks Ago" not in seen["titles"]
