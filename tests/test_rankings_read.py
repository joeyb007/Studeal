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
