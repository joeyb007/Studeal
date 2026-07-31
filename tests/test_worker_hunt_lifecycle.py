"""Tests for hunt lifecycle bookkeeping in the worker.

`_run_hunt_and_persist` is tested directly (async, no Celery) with the hunt
pipeline, persistence, and event publisher all replaced — this file verifies
the bookkeeping contract: Hunt rows, watchlist.last_hunt_at, event ordering,
and the failure path.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Hunt, User, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.persistence.listings import PersistResult


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str):
        self.published.append((channel, message))

    async def aclose(self):
        pass


@pytest.fixture()
async def rig(monkeypatch):
    """sqlite session factory patched into the worker module + fake publisher."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as s:
            yield s

    import dealbot.persistence.listings as listings_mod
    import dealbot.recsys.gate as gate_mod
    import dealbot.worker.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "get_async_session", _session)
    # gate and persistence open their own sessions; without these patches
    # they dial the real DATABASE_URL. The rig watchlist has no
    # intent_embedding, so the real pool_candidates returns [] →
    # pre-existing tests take the full-hunt path; the real
    # mark_new_for_watchlist novelty logic runs against sqlite.
    monkeypatch.setattr(gate_mod, "get_async_session", _session)
    monkeypatch.setattr(listings_mod, "get_async_session", _session)

    fake = FakeRedis()
    monkeypatch.setattr(tasks_mod, "_get_publisher", lambda: RedisEventPublisher(client=fake))
    monkeypatch.setattr(tasks_mod, "_maybe_governor", lambda: None)
    monkeypatch.setattr(tasks_mod, "_maybe_dispatch_alerts", lambda hunt_id: None)

    async with factory() as s:
        user = User(email="w@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="aeron", context='{"product_query": "aeron"}')
        s.add(wl)
        await s.flush()
        wl_id = wl.id
        await s.commit()

    yield factory, fake, wl_id, tasks_mod, monkeypatch
    await engine.dispose()


def _event_types(fake: FakeRedis) -> list[str]:
    return [json.loads(m)["type"] for _, m in fake.published]


@pytest.mark.asyncio
async def test_successful_hunt_writes_hunt_row_and_events(rig):
    factory, fake, wl_id, tasks_mod, monkeypatch = rig

    async def fake_run_hunt(spec, events=None):
        return [object(), object()]

    async def fake_persist(offers, hunt_id=None):
        assert hunt_id is not None
        return PersistResult(written=2, listing_ids=[1, 2], new_global_ids=[2])

    async def fake_mark_new(hunt_id):
        return [2]

    monkeypatch.setattr(tasks_mod, "run_hunt", fake_run_hunt)
    monkeypatch.setattr(tasks_mod, "persist_offers", fake_persist)
    monkeypatch.setattr(tasks_mod, "mark_new_for_watchlist", fake_mark_new)

    result = await tasks_mod._run_hunt_and_persist(wl_id)

    assert result["offer_count"] == 2
    assert result["persisted"] == 2
    assert result["new_for_watchlist"] == 1
    assert isinstance(result["hunt_id"], int)

    async with factory() as s:
        hunt = await s.get(Hunt, result["hunt_id"])
        assert hunt.status == "succeeded"
        assert hunt.offer_count == 2
        assert hunt.persisted_count == 2
        assert hunt.new_listing_count == 1
        assert hunt.finished_at is not None
        wl = await s.get(Watchlist, wl_id)
        assert wl.last_hunt_at == hunt.started_at

    types = _event_types(fake)
    assert types[0] == "hunt.started"
    assert "hunt.persisted" in types
    assert types[-1] == "hunt.finished"
    finished = json.loads(fake.published[-1][1])
    assert finished["status"] == "succeeded"
    assert finished["duration_s"] >= 0


@pytest.mark.asyncio
async def test_failed_hunt_marks_row_failed(rig):
    factory, fake, wl_id, tasks_mod, monkeypatch = rig

    async def exploding_run_hunt(spec, events=None):
        raise RuntimeError("browser died")

    monkeypatch.setattr(tasks_mod, "run_hunt", exploding_run_hunt)

    with pytest.raises(RuntimeError, match="browser died"):
        await tasks_mod._run_hunt_and_persist(wl_id)

    async with factory() as s:
        hunts = (await s.execute(
            select(Hunt).where(Hunt.watchlist_id == wl_id)
        )).scalars().all()
        assert len(hunts) == 1
        assert hunts[0].status == "failed"
        assert "browser died" in hunts[0].error
        assert hunts[0].finished_at is not None

    finished = json.loads(fake.published[-1][1])
    assert finished["type"] == "hunt.finished"
    assert finished["status"] == "failed"


@pytest.mark.asyncio
async def test_fleet_at_capacity_raises_before_any_row(rig):
    factory, fake, wl_id, tasks_mod, monkeypatch = rig

    class FullGovernor:
        async def has_capacity(self, pending: int = 0) -> bool:
            return False

    monkeypatch.setattr(tasks_mod, "_maybe_governor", lambda: FullGovernor())

    with pytest.raises(tasks_mod.FleetAtCapacity):
        await tasks_mod._run_hunt_and_persist(wl_id)

    async with factory() as s:
        hunts = (await s.execute(select(Hunt))).scalars().all()
        assert hunts == []          # no orphan row
    assert fake.published == []     # no events either


@pytest.mark.asyncio
async def test_flaky_governor_fails_open(rig):
    """Redis blips in the governor must never stop or fail a hunt."""
    factory, fake, wl_id, tasks_mod, monkeypatch = rig

    class FlakyGovernor:
        async def has_capacity(self, pending: int = 0) -> bool:
            raise ConnectionError("redis down")

        async def register(self, hunt_id: int) -> None:
            raise ConnectionError("redis down")

        async def deregister(self, hunt_id: int) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr(tasks_mod, "_maybe_governor", lambda: FlakyGovernor())

    async def fake_run_hunt(spec, events=None):
        return [object()]

    async def fake_persist(offers, hunt_id=None):
        return PersistResult(written=1, listing_ids=[1], new_global_ids=[1])

    async def fake_mark_new(hunt_id):
        return []

    monkeypatch.setattr(tasks_mod, "run_hunt", fake_run_hunt)
    monkeypatch.setattr(tasks_mod, "persist_offers", fake_persist)
    monkeypatch.setattr(tasks_mod, "mark_new_for_watchlist", fake_mark_new)

    result = await tasks_mod._run_hunt_and_persist(wl_id)
    assert result["offer_count"] == 1

    async with factory() as s:
        hunt = await s.get(Hunt, result["hunt_id"])
        assert hunt.status == "succeeded"


@pytest.mark.asyncio
async def test_missing_watchlist_short_circuits(rig):
    factory, fake, _, tasks_mod, _mp = rig
    result = await tasks_mod._run_hunt_and_persist(99999)
    assert result == {"watchlist_id": 99999, "error": "not_found"}
    assert fake.published == []


# ---------------------------------------------------------------------------
# Sufficiency gate (§8b)
# ---------------------------------------------------------------------------

def _pool_listing(n: int):
    from dealbot.db.models import Listing

    return Listing(
        canonical_url=f"pc{n}", raw_url=f"https://m.test/p{n}",
        marketplace="kijiji", title=f"Pool find {n}", price=50.0,
        currency="CAD", condition="used",
    )


@pytest.mark.asyncio
async def test_sufficient_pool_serves_a_cached_hunt(rig):
    """≥ k novel fresh matches → no browsing: run_hunt must not be called,
    the hunt closes as "cached", and alerts still fire on the novel rows."""
    factory, fake, wl_id, tasks_mod, monkeypatch = rig
    from dealbot.db.models import Listing as L

    async with factory() as s:
        listings = [_pool_listing(i) for i in range(10)]
        s.add_all(listings)
        await s.commit()
        ids = [l.id for l in listings]

    async def _candidates(watchlist_id, *, limit=50):
        async with factory() as s:
            return list((await s.execute(
                select(L).where(L.id.in_(ids))
            )).scalars().all())

    browsed = {"called": False}

    async def _no_browse(context, events=None):
        browsed["called"] = True
        return []

    dispatched: list[int] = []
    monkeypatch.setattr(tasks_mod, "pool_candidates", _candidates)
    monkeypatch.setattr(tasks_mod, "run_hunt", _no_browse)
    monkeypatch.setattr(tasks_mod, "_maybe_dispatch_alerts", dispatched.append)

    result = await tasks_mod._run_hunt_and_persist(wl_id)

    assert browsed["called"] is False, "sufficient pool must not browse"
    async with factory() as s:
        hunt = await s.get(Hunt, result["hunt_id"])
        wl = await s.get(Watchlist, wl_id)
    assert hunt.status == "cached"
    assert hunt.new_listing_count == 10, "pool rows are novel on first serve"
    assert wl.last_hunt_at is not None, "cached refresh must reset cadence"
    assert dispatched == [hunt.id], "alerts are never cached"

    finished = [m for _c, m in fake.published if '"hunt.finished"' in m]
    assert finished and '"cached"' in finished[-1]


@pytest.mark.asyncio
async def test_insufficient_pool_runs_the_full_hunt_and_merges(rig):
    """< k matches → full hunt, and the few pool matches still become alert
    candidates (pool-first alerts in BOTH branches)."""
    factory, fake, wl_id, tasks_mod, monkeypatch = rig
    from dealbot.db.models import HuntListing, Listing as L

    async with factory() as s:
        pool_row = _pool_listing(99)
        s.add(pool_row)
        await s.commit()
        pool_id = pool_row.id

    async def _thin_candidates(watchlist_id, *, limit=50):
        async with factory() as s:
            return list((await s.execute(
                select(L).where(L.id == pool_id)
            )).scalars().all())

    browsed = {"called": False}

    async def _browse(context, events=None):
        browsed["called"] = True
        return []

    async def _persist(offers, hunt_id=None):
        return PersistResult(written=0)

    monkeypatch.setattr(tasks_mod, "pool_candidates", _thin_candidates)
    monkeypatch.setattr(tasks_mod, "run_hunt", _browse)
    monkeypatch.setattr(tasks_mod, "persist_offers", _persist)

    result = await tasks_mod._run_hunt_and_persist(wl_id)

    assert browsed["called"] is True, "1 < k → hunt"
    async with factory() as s:
        links = (await s.execute(
            select(HuntListing).where(HuntListing.hunt_id == result["hunt_id"])
        )).scalars().all()
    assert [(l.listing_id, l.source) for l in links] == [(pool_id, "pool")], (
        "the thin pool match must still ride along as an alert candidate"
    )
    async with factory() as s:
        hunt = await s.get(Hunt, result["hunt_id"])
    assert hunt.status == "succeeded"


@pytest.mark.asyncio
async def test_pro_user_always_hunts_live(rig):
    """Freshness is the paid feature: Pro skips the gate entirely."""
    factory, fake, wl_id, tasks_mod, monkeypatch = rig

    async with factory() as s:
        user = (await s.execute(select(User))).scalars().one()
        user.is_pro = True
        await s.commit()

    gate_called = {"n": 0}

    async def _candidates(watchlist_id, *, limit=50):
        gate_called["n"] += 1
        return []

    async def _browse(context, events=None):
        return []

    async def _persist(offers, hunt_id=None):
        return PersistResult(written=0)

    monkeypatch.setattr(tasks_mod, "pool_candidates", _candidates)
    monkeypatch.setattr(tasks_mod, "run_hunt", _browse)
    monkeypatch.setattr(tasks_mod, "persist_offers", _persist)

    result = await tasks_mod._run_hunt_and_persist(wl_id)

    assert gate_called["n"] == 0, "pro never consults the gate"
    async with factory() as s:
        hunt = await s.get(Hunt, result["hunt_id"])
    assert hunt.status == "succeeded"
