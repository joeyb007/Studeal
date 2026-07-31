"""Sufficiency gate — the pool read that decides how much browsing a refresh
does. Fails toward hunting: no intent vector or an empty pool must read as
insufficient, never as "sufficient with nothing to show"."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Hunt, HuntListing, Listing, User, Watchlist

NOW = datetime.now(timezone.utc)


@pytest.fixture()
async def rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session():
        async with factory() as s:
            yield s

    import dealbot.recsys.gate as gate_mod

    monkeypatch.setattr(gate_mod, "get_async_session", _session)
    yield factory, gate_mod
    await engine.dispose()


def _listing(n: int) -> Listing:
    return Listing(
        canonical_url=f"c{n}", raw_url=f"https://m.test/{n}", marketplace="kijiji",
        title=f"Item {n}", price=100.0, currency="CAD", condition="used",
        first_seen_at=NOW, last_seen_at=NOW,
    )


def test_constants_default_to_measured_values():
    from dealbot.recsys.gate import GATE_SIMILARITY_TAU, GATE_SUFFICIENCY_K

    assert GATE_SIMILARITY_TAU == 0.50, "measured 2026-07-31 — change via env only"
    assert GATE_SUFFICIENCY_K == 10


@pytest.mark.asyncio
async def test_no_intent_vector_reads_as_insufficient(rig):
    factory, gate = rig
    async with factory() as s:
        user = User(email="u@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="w", context='{"product_query": "x"}')
        s.add(wl)
        await s.commit()
        wl_id = wl.id

    assert await gate.pool_candidates(wl_id) == [], (
        "no vector → no gate → always hunt (fail toward hunting)"
    )


@pytest.mark.asyncio
async def test_link_inserts_pool_rows_and_skips_already_linked(rig):
    factory, gate = rig
    async with factory() as s:
        user = User(email="u@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="w")
        s.add(wl)
        await s.flush()
        hunt = Hunt(watchlist_id=wl.id)
        a, b = _listing(1), _listing(2)
        s.add_all([hunt, a, b])
        await s.flush()
        # `a` was already found by this hunt's own browsing.
        s.add(HuntListing(hunt_id=hunt.id, listing_id=a.id))
        await s.commit()
        hunt_id, a_id, b_id = hunt.id, a.id, b.id

    inserted = await gate.link_pool_candidates(hunt_id, [a_id, b_id])
    assert inserted == 1, "the browsed duplicate must be skipped, not conflict"

    async with factory() as s:
        rows = (await s.execute(
            select(HuntListing).where(HuntListing.hunt_id == hunt_id)
        )).scalars().all()
    by_listing = {r.listing_id: r.source for r in rows}
    assert by_listing == {a_id: "browsed", b_id: "pool"}


@pytest.mark.asyncio
async def test_link_with_no_ids_is_a_noop(rig):
    _factory, gate = rig
    assert await gate.link_pool_candidates(999, []) == 0
