"""Pool read surface — diversified browse feed + natural-language search.

The feed's job is not "newest first": hunts write in bursts, so strict recency
buries the pool's breadth under one agent's 100 near-identical listings.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Hunt, HuntListing, Listing, User, Watchlist

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def pool_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr(
        "dealbot.api.routes.listings_feed.get_async_session", _test_session,
    )
    return authed_client, factory, monkeypatch


async def _seed_pool(factory) -> None:
    """One bursty watchlist (30 kijiji listings, newest) + two small ones.
    Strict recency would fill page one entirely with the burst."""
    async with factory() as s:
        user = User(id=1, email="test@example.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wls = [Watchlist(user_id=1, name=n) for n in ("burst", "small-a", "small-b")]
        s.add_all(wls)
        await s.flush()
        hunts = [Hunt(watchlist_id=w.id) for w in wls]
        s.add_all(hunts)
        await s.flush()

        plan = [(hunts[0], "kijiji", 30), (hunts[1], "ebay", 3), (hunts[2], "craigslist", 3)]
        n = 0
        for hunt, marketplace, count in plan:
            for _ in range(count):
                n += 1
                listing = Listing(
                    canonical_url=f"c{n}", raw_url=f"https://m.test/{n}",
                    marketplace=marketplace, title=f"Item {n}", price=100.0 + n,
                    currency="CAD", condition="used",
                    first_seen_at=NOW - timedelta(hours=1),
                    last_seen_at=NOW - timedelta(minutes=n),
                )
                s.add(listing)
                await s.flush()
                s.add(HuntListing(hunt_id=hunt.id, listing_id=listing.id))
        await s.commit()


@pytest.mark.asyncio
async def test_feed_diversifies_across_sources(pool_client):
    client, factory, _ = pool_client
    await _seed_pool(factory)
    data = client.get("/listings/feed", params={"limit": 12}).json()
    marketplaces = [row["marketplace"] for row in data["listings"]]

    assert len(data["listings"]) == 12
    assert len(set(marketplaces)) >= 3, f"one source dominated page one: {marketplaces}"

    # The burst (30 kijiji) must not own the top of the page. The cap can only
    # hold while >= 2 marketplaces still have stock — once the small sources are
    # exhausted, filling with what's left beats truncating the page.
    # ebay and craigslist hold 3 each, so round-robin keeps all three sources
    # in play for the first 9 slots; after that only the burst has stock left.
    contested = marketplaces[:9]
    run = 1
    for prev, cur in zip(contested, contested[1:]):
        run = run + 1 if cur == prev else 1
        assert run <= 3, f"more than 3 consecutive while diversification was possible: {contested}"
    assert marketplaces[:3].count("kijiji") <= 1, (
        f"burst dominated the very top of the feed: {marketplaces[:6]}"
    )


@pytest.mark.asyncio
async def test_feed_filters(pool_client):
    client, factory, _ = pool_client
    await _seed_pool(factory)
    rows = client.get("/listings/feed", params={"marketplace": "ebay"}).json()["listings"]
    assert rows and all(r["marketplace"] == "ebay" for r in rows)

    cheap = client.get("/listings/feed", params={"max_price": 105}).json()["listings"]
    assert cheap and all(r["price"] <= 105 for r in cheap)


@pytest.mark.asyncio
async def test_feed_respects_recency_window(pool_client):
    client, factory, _ = pool_client
    await _seed_pool(factory)
    async with factory() as s:
        s.add(Listing(
            canonical_url="old", raw_url="https://m.test/old",
            marketplace="kijiji", title="Ancient", price=1.0,
            currency="CAD", condition="used",
            first_seen_at=NOW - timedelta(days=40),
            last_seen_at=NOW - timedelta(days=40),
        ))
        await s.commit()
    titles = [
        r["title"]
        for r in client.get("/listings/feed", params={"days": 7}).json()["listings"]
    ]
    assert "Ancient" not in titles


@pytest.mark.integration   # pgvector cosine_distance has no sqlite equivalent
@pytest.mark.asyncio
async def test_search_semantic_path(pool_client):
    client, factory, mp = pool_client
    await _seed_pool(factory)

    async def fake_embed_text(q):
        return [0.1] * 1536

    mp.setattr("dealbot.api.routes.listings_feed.embed_text", fake_embed_text)
    data = client.get("/listings/search", params={"q": "a comfy office chair"}).json()
    assert data["semantic"] is True
    assert isinstance(data["listings"], list)


@pytest.mark.asyncio
async def test_search_falls_back_to_ilike(pool_client):
    client, factory, mp = pool_client
    await _seed_pool(factory)

    async def no_embedding(q):
        return []

    mp.setattr("dealbot.api.routes.listings_feed.embed_text", no_embedding)
    data = client.get("/listings/search", params={"q": "Item 1"}).json()
    assert data["semantic"] is False
    assert data["listings"], "fallback must still return keyword matches"


def test_endpoints_require_auth(client):
    assert client.get("/listings/feed").status_code == 401
    assert client.get("/listings/search", params={"q": "x"}).status_code == 401
