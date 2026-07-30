"""Tests for FleetGovernor — Redis-backed hunt concurrency governance.

Uses an in-file fake Redis (dict-backed zset/kv) so no live Redis is needed;
the governor's logic is what's under test, not redis-py.
"""

from __future__ import annotations

import time

import pytest

from dealbot.worker.governor import FleetGovernor


class FakeRedis:
    def __init__(self):
        self.zsets: dict[str, dict[str, float]] = {}
        self.kv: dict[str, str] = {}

    async def zadd(self, name: str, mapping: dict[str, float]):
        self.zsets.setdefault(name, {}).update(mapping)

    async def zrem(self, name: str, *members: str):
        for m in members:
            self.zsets.get(name, {}).pop(m, None)

    async def zcard(self, name: str) -> int:
        return len(self.zsets.get(name, {}))

    async def zremrangebyscore(self, name: str, min: float | str, max: float | str):
        lo = float("-inf") if min in ("-inf", float("-inf")) else float(min)
        hi = float("inf") if max in ("+inf", float("inf")) else float(max)
        zset = self.zsets.get(name, {})
        for member in [m for m, score in zset.items() if lo <= score <= hi]:
            del zset[member]

    async def set(self, name: str, value, nx: bool = False, ex: int | None = None):
        if nx and name in self.kv:
            return None
        self.kv[name] = str(value)
        return True

    async def get(self, name: str):
        val = self.kv.get(name)
        return val.encode() if val is not None else None

    async def delete(self, name: str):
        self.kv.pop(name, None)


def _governor(fake: FakeRedis, max_concurrent: int = 2, gap: int = 20) -> FleetGovernor:
    return FleetGovernor(fake, max_concurrent=max_concurrent, min_start_gap_s=gap)


@pytest.mark.asyncio
async def test_capacity_respects_max():
    fake = FakeRedis()
    gov = _governor(fake, max_concurrent=2)
    assert await gov.has_capacity() is True
    await gov.register(1)
    await gov.register(2)
    assert await gov.active_count() == 2
    assert await gov.has_capacity() is False


@pytest.mark.asyncio
async def test_pending_counts_against_capacity():
    fake = FakeRedis()
    gov = _governor(fake, max_concurrent=2)
    await gov.register(1)
    assert await gov.has_capacity() is True
    assert await gov.has_capacity(pending=1) is False


@pytest.mark.asyncio
async def test_stale_entries_pruned():
    fake = FakeRedis()
    gov = _governor(fake)
    # A hunt registered 1000s ago — beyond STALE_S (900) — must not count.
    await fake.zadd(FleetGovernor.ZSET_KEY, {"42": time.time() - 1000})
    assert await gov.active_count() == 0


@pytest.mark.asyncio
async def test_deregister_frees_capacity():
    fake = FakeRedis()
    gov = _governor(fake, max_concurrent=1)
    await gov.register(7)
    assert await gov.has_capacity() is False
    await gov.deregister(7)
    assert await gov.has_capacity() is True


@pytest.mark.asyncio
async def test_seconds_since_last_start():
    fake = FakeRedis()
    gov = _governor(fake)
    assert await gov.seconds_since_last_start() == float("inf")
    await gov.register(1)
    assert await gov.seconds_since_last_start() < 5.0


@pytest.mark.asyncio
async def test_tick_lock_mutual_exclusion():
    fake = FakeRedis()
    gov_a = _governor(fake)
    gov_b = _governor(fake)
    assert await gov_a.acquire_tick_lock() is True
    assert await gov_b.acquire_tick_lock() is False  # held by A
    await gov_a.release_tick_lock()
    assert await gov_b.acquire_tick_lock() is True


@pytest.mark.asyncio
async def test_env_defaults(monkeypatch):
    monkeypatch.setenv("FLEET_MAX_CONCURRENT_HUNTS", "5")
    monkeypatch.setenv("FLEET_MIN_START_GAP_S", "45")
    gov = FleetGovernor(FakeRedis())
    assert gov.max_concurrent == 5
    assert gov.min_start_gap_s == 45
