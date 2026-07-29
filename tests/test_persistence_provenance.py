"""Tests for hunt provenance in listing persistence.

Covers the PersistResult identities returned by `persist_offers`, hunt linking
via `hunt_listings`, and the `mark_new_for_watchlist` helper. Same in-memory
SQLite + patched `get_async_session` rig as test_persistence_listings.py.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.workers.extractor import Offer
from dealbot.db.models import Base, Hunt, HuntListing, Listing, User, Watchlist


@pytest.fixture()
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with f() as session:
            yield session

    monkeypatch.setattr(
        "dealbot.persistence.listings.get_async_session", _test_session,
    )
    yield f
    await engine.dispose()


def _offer(url: str, marketplace: str = "kijiji", **kw) -> Offer:
    defaults = dict(
        title="Aeron chair", price=500.0, currency="CAD",
        url=url, marketplace=marketplace,
    )
    defaults.update(kw)
    return Offer(**defaults)


async def _seed(f):
    """User + watchlist + two listings; l1 seen in an older hunt, both linked
    to a newer hunt. Returns (watchlist_id, old_hunt_id, new_hunt_id, l1_id, l2_id)."""
    async with f() as s:
        user = User(email="t@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="aeron")
        s.add(wl)
        await s.flush()
        l1 = Listing(canonical_url="c1", raw_url="c1", marketplace="kijiji", title="a", price=1.0)
        l2 = Listing(canonical_url="c2", raw_url="c2", marketplace="kijiji", title="b", price=2.0)
        s.add_all([l1, l2])
        await s.flush()
        t0 = datetime.now(timezone.utc) - timedelta(hours=2)
        old = Hunt(watchlist_id=wl.id, started_at=t0)
        new = Hunt(watchlist_id=wl.id)
        s.add_all([old, new])
        await s.flush()
        s.add_all([
            HuntListing(hunt_id=old.id, listing_id=l1.id),
            HuntListing(hunt_id=new.id, listing_id=l1.id),
            HuntListing(hunt_id=new.id, listing_id=l2.id),
        ])
        ids = (wl.id, old.id, new.id, l1.id, l2.id)
        await s.commit()
        return ids


@pytest.mark.asyncio
async def test_mark_new_only_flags_unseen_listings(factory):
    from dealbot.persistence.listings import mark_new_for_watchlist
    _, _, new_hunt, l1, l2 = await _seed(factory)
    marked = await mark_new_for_watchlist(new_hunt)
    assert marked == [l2]
    async with factory() as s:
        rows = (
            await s.execute(select(HuntListing).where(HuntListing.hunt_id == new_hunt))
        ).scalars().all()
        flags = {r.listing_id: r.was_new_for_watchlist for r in rows}
        assert flags == {l1: False, l2: True}


@pytest.mark.asyncio
async def test_mark_new_is_idempotent(factory):
    from dealbot.persistence.listings import mark_new_for_watchlist
    _, _, new_hunt, _, l2 = await _seed(factory)
    first = await mark_new_for_watchlist(new_hunt)
    second = await mark_new_for_watchlist(new_hunt)
    assert first == [l2] and second == [l2]


@pytest.mark.asyncio
async def test_mark_new_missing_hunt_returns_empty(factory):
    from dealbot.persistence.listings import mark_new_for_watchlist
    assert await mark_new_for_watchlist(99999) == []


@pytest.mark.asyncio
async def test_persist_returns_identities_and_new_global(factory):
    from dealbot.persistence.listings import persist_offers
    result = await persist_offers([
        _offer("https://www.kijiji.ca/v-office/aeron/1"),
        _offer("https://www.kijiji.ca/v-office/aeron/2"),
    ])
    assert result.written == 2
    assert len(result.listing_ids) == 2
    assert sorted(result.new_global_ids) == sorted(result.listing_ids)

    # Re-persisting one URL: it is an update, not globally new.
    again = await persist_offers([_offer("https://www.kijiji.ca/v-office/aeron/1")])
    assert again.written == 1
    assert again.new_global_ids == []
    assert again.listing_ids != []


@pytest.mark.asyncio
async def test_persist_links_hunt(factory):
    from dealbot.persistence.listings import persist_offers
    async with factory() as s:
        user = User(email="h@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="aeron")
        s.add(wl)
        await s.flush()
        hunt = Hunt(watchlist_id=wl.id)
        s.add(hunt)
        await s.flush()
        hunt_id = hunt.id
        await s.commit()

    result = await persist_offers(
        [_offer("https://www.kijiji.ca/v-office/aeron/9")], hunt_id=hunt_id,
    )
    async with factory() as s:
        links = (
            await s.execute(select(HuntListing).where(HuntListing.hunt_id == hunt_id))
        ).scalars().all()
    assert [link.listing_id for link in links] == result.listing_ids
    assert all(link.was_new_for_watchlist is False for link in links)


@pytest.mark.asyncio
async def test_persist_empty_returns_empty_result(factory):
    from dealbot.persistence.listings import persist_offers
    result = await persist_offers([])
    assert result.written == 0
    assert result.listing_ids == [] and result.new_global_ids == []
