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

    import dealbot.worker.tasks as tasks_mod

    monkeypatch.setattr(tasks_mod, "get_async_session", _session)

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
