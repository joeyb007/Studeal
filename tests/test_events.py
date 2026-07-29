"""Tests for the hunt event schema + Redis publisher.

The publisher is the write side of the SSE contract Mission Control consumes:
JSON envelopes on channel events:watchlist:{id}. It must never raise — a
dead Redis must not break a hunt.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import ExplorerTurn, HuntStarted, channel_for


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str):
        self.published.append((channel, message))

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_publish_sends_json_to_watchlist_channel():
    fake = FakeRedis()
    pub = RedisEventPublisher(client=fake)
    await pub.publish(HuntStarted(hunt_id=7, watchlist_id=3))
    (channel, message), = fake.published
    assert channel == channel_for(3) == "events:watchlist:3"
    body = json.loads(message)
    assert body["type"] == "hunt.started"
    assert body["hunt_id"] == 7
    assert body["v"] == 1
    assert "ts" in body


@pytest.mark.asyncio
async def test_publish_swallows_redis_errors():
    class Boom(FakeRedis):
        async def publish(self, c, m):
            raise ConnectionError("down")

    pub = RedisEventPublisher(client=Boom())
    await pub.publish(HuntStarted(hunt_id=1, watchlist_id=1))  # must not raise


@pytest.mark.asyncio
async def test_publish_nowait_schedules_task():
    fake = FakeRedis()
    pub = RedisEventPublisher(client=fake)
    pub.publish_nowait(ExplorerTurn(
        hunt_id=1, watchlist_id=2, query="q", marketplace="kijiji",
        turn=1, url="https://k.ca", action="click", result="ok",
    ))
    await asyncio.sleep(0)
    assert len(fake.published) == 1
    body = json.loads(fake.published[0][1])
    assert body["type"] == "explorer.turn" and body["marketplace"] == "kijiji"


def test_publish_nowait_without_loop_drops_silently():
    fake = FakeRedis()
    pub = RedisEventPublisher(client=fake)
    # No running loop in a sync test — must not raise, must not publish.
    pub.publish_nowait(HuntStarted(hunt_id=1, watchlist_id=1))
    assert fake.published == []
