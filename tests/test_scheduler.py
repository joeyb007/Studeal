"""Tests for schedule_due_hunts — the fleet's cadence brain.

`_schedule` is tested directly with an injected FakeGovernor and enqueue stub;
tier cadence (free daily / pro hourly), due-selection, capacity stops, stagger
countdowns, and optimistic last_hunt_at are the contract.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Hunt, User, Watchlist
from dealbot.worker.governor import FleetGovernor

NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)
CONTEXT = '{"product_query": "aeron"}'


class FakeGovernor:
    def __init__(self, capacity: int, gap: int = 20, lock_available: bool = True):
        self.capacity = capacity
        self.min_start_gap_s = gap
        self.lock_available = lock_available

    async def has_capacity(self, pending: int = 0) -> bool:
        return pending < self.capacity

    async def acquire_tick_lock(self, ttl_s: int = 240) -> bool:
        return self.lock_available

    async def release_tick_lock(self) -> None:
        pass


@pytest.fixture()
async def rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as s:
            yield s

    import dealbot.worker.scheduler as sched_mod

    monkeypatch.setattr(sched_mod, "get_async_session", _session)
    yield factory, sched_mod
    await engine.dispose()


_seq = iter(range(10_000))


async def _seed(factory, *, is_pro: bool, last_hunt_at=None, enabled=True,
                context=CONTEXT, expires_at=None, email=None) -> int:
    async with factory() as s:
        user = User(email=email or f"u{next(_seq)}@t.com",
                    hashed_password="x", is_pro=is_pro)
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="wl", context=context,
                       hunting_enabled=enabled, last_hunt_at=last_hunt_at,
                       expires_at=expires_at)
        s.add(wl)
        await s.flush()
        wl_id = wl.id
        await s.commit()
        return wl_id


def _run(sched_mod, governor, enqueued):
    async def enqueue(wl_id: int, countdown: int):
        enqueued.append((wl_id, countdown))
    return sched_mod._schedule(now=NOW, governor=governor, enqueue=enqueue)


@pytest.mark.asyncio
async def test_pro_user_hourly_free_user_daily(rig):
    factory, sched_mod = rig
    two_hours_ago = NOW - timedelta(hours=2)
    pro_wl = await _seed(factory, is_pro=True, last_hunt_at=two_hours_ago)
    await _seed(factory, is_pro=False, last_hunt_at=two_hours_ago)

    enqueued: list[tuple[int, int]] = []
    result = await _run(sched_mod, FakeGovernor(capacity=10), enqueued)

    assert result["due"] == 1
    assert [wl for wl, _ in enqueued] == [pro_wl]


@pytest.mark.asyncio
async def test_never_hunted_watchlist_is_due(rig):
    factory, sched_mod = rig
    wl = await _seed(factory, is_pro=False, last_hunt_at=None)
    enqueued: list[tuple[int, int]] = []
    result = await _run(sched_mod, FakeGovernor(capacity=10), enqueued)
    assert result["enqueued"] == 1
    assert enqueued[0][0] == wl


@pytest.mark.asyncio
async def test_disabled_expired_and_contextless_skipped(rig):
    factory, sched_mod = rig
    await _seed(factory, is_pro=True, enabled=False)
    await _seed(factory, is_pro=True, expires_at=NOW - timedelta(days=1))
    await _seed(factory, is_pro=True, context=None)
    enqueued: list[tuple[int, int]] = []
    result = await _run(sched_mod, FakeGovernor(capacity=10), enqueued)
    assert result == {"enqueued": 0, "due": 0, "skipped_capacity": 0}


@pytest.mark.asyncio
async def test_capacity_stops_enqueueing(rig):
    factory, sched_mod = rig
    for _ in range(3):
        await _seed(factory, is_pro=True, last_hunt_at=None)
    enqueued: list[tuple[int, int]] = []
    result = await _run(sched_mod, FakeGovernor(capacity=2), enqueued)
    assert result["enqueued"] == 2
    assert result["skipped_capacity"] == 1


@pytest.mark.asyncio
async def test_stagger_countdowns(rig):
    factory, sched_mod = rig
    for _ in range(3):
        await _seed(factory, is_pro=True, last_hunt_at=None)
    enqueued: list[tuple[int, int]] = []
    await _run(sched_mod, FakeGovernor(capacity=10, gap=30), enqueued)
    assert [cd for _, cd in enqueued] == [0, 30, 60]


@pytest.mark.asyncio
async def test_lock_held_skips_tick(rig):
    factory, sched_mod = rig
    await _seed(factory, is_pro=True, last_hunt_at=None)
    enqueued: list[tuple[int, int]] = []
    result = await _run(sched_mod, FakeGovernor(capacity=10, lock_available=False), enqueued)
    assert result == {"enqueued": 0, "due": 0, "skipped_capacity": 0}
    assert enqueued == []


@pytest.mark.asyncio
async def test_stale_running_hunts_reaped(rig):
    factory, sched_mod = rig
    wl = await _seed(factory, is_pro=True, last_hunt_at=NOW)  # not due
    async with factory() as s:
        stale = Hunt(watchlist_id=wl, started_at=NOW - timedelta(seconds=FleetGovernor.STALE_S + 100))
        fresh = Hunt(watchlist_id=wl, started_at=NOW - timedelta(seconds=60))
        s.add_all([stale, fresh])
        await s.flush()
        stale_id, fresh_id = stale.id, fresh.id
        await s.commit()

    await _run(sched_mod, FakeGovernor(capacity=10), [])

    async with factory() as s:
        reaped = await s.get(Hunt, stale_id)
        assert reaped.status == "failed"
        assert "reaped" in reaped.error
        assert reaped.finished_at is not None
        untouched = await s.get(Hunt, fresh_id)
        assert untouched.status == "running"


@pytest.mark.asyncio
async def test_oldest_hunted_first_and_optimistic_last_hunt_at(rig):
    factory, sched_mod = rig
    older = await _seed(factory, is_pro=True, last_hunt_at=NOW - timedelta(hours=5))
    never = await _seed(factory, is_pro=True, last_hunt_at=None)
    newer = await _seed(factory, is_pro=True, last_hunt_at=NOW - timedelta(hours=2))

    enqueued: list[tuple[int, int]] = []
    await _run(sched_mod, FakeGovernor(capacity=10), enqueued)

    assert [wl for wl, _ in enqueued] == [never, older, newer]

    async with factory() as s:
        for wl_id in (older, never, newer):
            wl = await s.get(Watchlist, wl_id)
            got = wl.last_hunt_at
            if got.tzinfo is None:
                got = got.replace(tzinfo=timezone.utc)
            assert got == NOW
