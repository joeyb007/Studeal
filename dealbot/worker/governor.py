"""FleetGovernor — global hunt-concurrency governance in Redis.

Politeness note: every public hunt targets the same marketplace lineup
(Kijiji/eBay/Craigslist), so the global start-gap below IS the
per-marketplace politeness interval — no per-site keys needed.

Active-hunt tracking: ZSET `fleet:active_hunts`, member=str(hunt_id),
score=unix start time. Entries older than STALE_S are pruned on read — a
crashed worker can never wedge the fleet.
"""

from __future__ import annotations

import os
import time

import redis.asyncio as aioredis


class FleetGovernor:
    ZSET_KEY = "fleet:active_hunts"
    LAST_START_KEY = "fleet:last_start"
    TICK_LOCK_KEY = "fleet:tick_lock"
    # Must exceed HUNT_BROWSE_DEADLINE_S (1200) plus persist/embed slack, or a
    # long hunt is evicted (and reaped as failed) while still legally running.
    STALE_S = 1800

    def __init__(
        self,
        client: "aioredis.Redis",
        *,
        max_concurrent: int | None = None,
        min_start_gap_s: int | None = None,
    ) -> None:
        self._client = client
        self.max_concurrent = (
            max_concurrent
            if max_concurrent is not None
            else int(os.environ.get("FLEET_MAX_CONCURRENT_HUNTS", "2"))
        )
        self.min_start_gap_s = (
            min_start_gap_s
            if min_start_gap_s is not None
            else int(os.environ.get("FLEET_MIN_START_GAP_S", "20"))
        )

    async def active_count(self) -> int:
        """Prune stale entries, then count live hunts."""
        await self._client.zremrangebyscore(
            self.ZSET_KEY, "-inf", time.time() - self.STALE_S,
        )
        return await self._client.zcard(self.ZSET_KEY)

    async def has_capacity(self, pending: int = 0) -> bool:
        return (await self.active_count()) + pending < self.max_concurrent

    async def register(self, hunt_id: int) -> None:
        now = time.time()
        await self._client.zadd(self.ZSET_KEY, {str(hunt_id): now})
        await self._client.set(self.LAST_START_KEY, now)

    async def deregister(self, hunt_id: int) -> None:
        await self._client.zrem(self.ZSET_KEY, str(hunt_id))

    async def acquire_tick_lock(self, ttl_s: int = 240) -> bool:
        """SET NX mutex for the scheduler tick — prevents concurrent
        schedule_due_hunts executions from double-enqueuing the same due set."""
        return bool(await self._client.set(self.TICK_LOCK_KEY, "1", nx=True, ex=ttl_s))

    async def release_tick_lock(self) -> None:
        await self._client.delete(self.TICK_LOCK_KEY)

    async def seconds_since_last_start(self) -> float:
        raw = await self._client.get(self.LAST_START_KEY)
        if raw is None:
            return float("inf")
        value = raw.decode() if isinstance(raw, bytes) else raw
        return time.time() - float(value)


_governor_cache: dict[str, object] = {"loop": None, "governor": None}


def build_governor() -> FleetGovernor:
    """Per-event-loop governor over $REDIS_URL — Celery's per-task loops make
    any cached redis client loop-bound. Keyed by loop identity with a held
    reference (see dealbot.db.database)."""
    import asyncio

    loop = asyncio.get_running_loop()
    if _governor_cache["loop"] is not loop:
        _governor_cache["loop"] = loop
        url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        _governor_cache["governor"] = FleetGovernor(aioredis.from_url(url))
    return _governor_cache["governor"]  # type: ignore[return-value]
