"""SSE hunt-event stream — the read side of the Mission Control contract.

Subscribes the caller to `events:watchlist:{id}` and forwards each pub/sub
message as one SSE `data:` line, with `: ping` heartbeats on 15s quiet
periods. Auth is a JWT in the `token` query param because EventSource cannot
set headers. Pub/sub has no replay: clients render DB state on load and treat
this stream as live augmentation.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse
from jose import JWTError, jwt

from dealbot.api.auth import ALGORITHM, SECRET_KEY
from dealbot.db.database import get_async_session
from dealbot.db.models import Watchlist
from dealbot.events.schema import channel_for

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/stream", tags=["stream"])

_HEARTBEAT_TIMEOUT_S = 15.0


def _get_pubsub(watchlist_id: int):
    """Seam for tests. One pubsub connection per stream."""
    url = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
    return aioredis.from_url(url).pubsub()


@router.get("/watchlists/{watchlist_id}")
async def stream_watchlist_events(
    watchlist_id: int,
    token: str = Query(...),
) -> StreamingResponse:
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        user_id = int(payload["sub"])
    except (JWTError, KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token")

    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
        if watchlist is None or watchlist.user_id != user_id:
            raise HTTPException(status_code=404, detail="Watchlist not found")

    pubsub = _get_pubsub(watchlist_id)

    async def event_stream() -> AsyncGenerator[str, None]:
        await pubsub.subscribe(channel_for(watchlist_id))
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=_HEARTBEAT_TIMEOUT_S,
                )
                if message is None:
                    yield ": ping\n\n"
                    continue
                data = message["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield f"data: {data}\n\n"
        finally:
            try:
                await pubsub.unsubscribe(channel_for(watchlist_id))
                await pubsub.aclose()
            except Exception:
                logger.debug("stream: pubsub close failed", exc_info=True)

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
