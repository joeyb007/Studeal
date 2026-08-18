"""The rankings READ path re-checks listing liveness: stale or sold listings
must never linger in a user's top picks between recomputes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from dealbot.db.models import Listing, User, Watchlist, WatchlistRanking


def _listing(canonical: str, *, stale=False, sold=False) -> Listing:
    now = datetime.now(timezone.utc)
    return Listing(
        canonical_url=canonical, raw_url=canonical, marketplace="kijiji",
        title=f"item {canonical}", price=100.0, currency="CAD", condition="used",
        first_seen_at=now - timedelta(days=10),
        last_seen_at=now - timedelta(days=10 if stale else 0),
        sold_at=now if sold else None,
    )


@pytest.mark.asyncio
async def test_stale_and_sold_leave_top_picks(authed_client, db_factory, monkeypatch):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _s():
        async with db_factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.watchlists.get_async_session", _s)

    async with db_factory() as session:
        user = User(email="test@example.com", hashed_password="x")
        user.id = 1
        session.add(user)
        await session.flush()
        wl = Watchlist(user_id=1, name="Chairs", context='{"product_query": "chair"}')
        session.add(wl)
        await session.flush()
        # Picks stay sealed until a sweep reports back; this agent has hunted.
        from dealbot.db.models import Hunt as _H

        session.add(_H(watchlist_id=wl.id, status="succeeded"))
        await session.flush()
        fresh = _listing("c-fresh")
        stale = _listing("c-stale", stale=True)
        sold = _listing("c-sold", sold=True)
        session.add_all([fresh, stale, sold])
        await session.flush()
        now = datetime.now(timezone.utc)
        for pos, listing in enumerate([sold, stale, fresh]):
            session.add(WatchlistRanking(
                watchlist_id=wl.id, listing_id=listing.id,
                score=0.9 - pos * 0.1, position=pos, computed_at=now,
            ))
        await session.commit()
        wl_id, fresh_id = wl.id, fresh.id

    resp = authed_client.get(f"/watchlists/{wl_id}/listings")
    assert resp.status_code == 200
    ids = [l["id"] for l in resp.json()["listings"]]
    assert ids == [fresh_id]          # gone listings gone from picks too


@pytest.mark.asyncio
async def test_zero_scored_candidates_are_not_persisted(monkeypatch):
    """Nearest-neighbour always returns something, so an agent whose item is
    absent from the pool gets handed unrelated listings. The ranker scores
    those 0; persisting them would make them durable "results" (golf-clubs
    agent surfaced office chairs, 2026-08-17). Zero means no match."""
    from dealbot.recsys import rank_cache
    from dealbot.recsys.ranker import RankedListing

    wl = Watchlist(user_id=1, name="Golf", context='{"product_query": "golf clubs"}')
    chair = _listing("c-chair")

    async def _fake_candidates(_watchlist, _context):
        return [chair]

    async def _fake_rank(_spec, candidates, **_kw):
        # What the real ranker does with an unrelated candidate.
        return [RankedListing(listing=c, score=0.0, reason="Not a match.") for c in candidates]

    monkeypatch.setattr(rank_cache, "_candidates", _fake_candidates)
    monkeypatch.setattr(rank_cache, "rank", _fake_rank)

    written: list = []

    class _Session:
        async def get(self, _model, _pk):
            return wl

        async def execute(self, _stmt):
            return None

        def add(self, row):
            written.append(row)

        async def commit(self):
            pass

    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session():
        yield _Session()

    monkeypatch.setattr(rank_cache, "get_async_session", _session)
    count = await rank_cache.recompute_rankings(1)
    assert count == 0
    assert written == []


@pytest.mark.asyncio
async def test_picks_are_sealed_until_a_sweep_reports_back(authed_client, db_factory, monkeypatch):
    """An agent that hasn't hunted has no picks BY DEFINITION. Ranking the
    shared pool for it invents results it never found and spends LLM budget
    doing so — a live agent was handed 150 pool rows mid-sweep (2026-08-18)."""
    from dealbot.db.models import Hunt, User, Watchlist
    from dealbot.recsys import rank_cache

    called = {"n": 0}

    async def _never(_wid):
        called["n"] += 1
        return 0

    monkeypatch.setattr("dealbot.api.routes.watchlists.recompute_rankings", _never)

    async with db_factory() as s:
        s.add(User(id=1, email="t@t.com", hashed_password="x"))
        await s.flush()
        fresh = Watchlist(user_id=1, name="Fresh", context='{"product_query": "x"}')
        hunted = Watchlist(user_id=1, name="Hunted", context='{"product_query": "y"}')
        s.add_all([fresh, hunted])
        await s.flush()
        s.add(Hunt(watchlist_id=hunted.id, status="succeeded"))
        await s.commit()
        fresh_id, hunted_id = fresh.id, hunted.id

    r = authed_client.get(f"/watchlists/{fresh_id}/listings").json()
    assert r["listings"] == []
    assert r["ranking_pending"] is True
    assert called["n"] == 0, "must not rank the shared pool before the first sweep"

    # An agent that HAS reported back still ranks normally.
    authed_client.get(f"/watchlists/{hunted_id}/listings")
    assert called["n"] == 1
