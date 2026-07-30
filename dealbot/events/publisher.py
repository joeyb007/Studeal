"""Redis pub/sub publisher for hunt events.

Fire-and-forget by design: a dead Redis, a missing loop, or a serialization
surprise must never break a hunt. Failures are logged and swallowed.
"""

from __future__ import annotations

import asyncio
import logging
import os

import redis.asyncio as aioredis

from dealbot.events.schema import Event, channel_for

logger = logging.getLogger(__name__)


class RedisEventPublisher:
    def __init__(
        self,
        redis_url: str | None = None,
        client: "aioredis.Redis | None" = None,
    ) -> None:
        """`client` is injectable for tests; otherwise connect lazily to
        `redis_url` (default: $REDIS_URL)."""
        self._url = redis_url or os.environ.get("REDIS_URL", "redis://localhost:6379/0")
        self._client = client
        # Strong refs to in-flight publish tasks: the loop keeps only weak
        # references, and a GC'd pending task is a silently dropped event.
        self._tasks: set[asyncio.Task] = set()

    def _get_client(self) -> "aioredis.Redis":
        if self._client is None:
            self._client = aioredis.from_url(self._url)
        return self._client

    async def publish(self, event: Event) -> None:
        """PUBLISH the event to its watchlist channel. Never raises."""
        try:
            await self._get_client().publish(
                channel_for(event.watchlist_id), event.model_dump_json(),
            )
        except Exception as exc:
            logger.warning("RedisEventPublisher: publish failed (%s): %s", event.type, exc)

    def publish_nowait(self, event: Event) -> None:
        """Schedule a publish from sync code running inside an event loop.
        With no running loop the event is dropped silently — trace-path
        callers must never block or crash on observability."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.publish(event))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
