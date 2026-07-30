"""Tests for the SSE hunt-event stream endpoint.

Auth rides a `?token=` query param (EventSource cannot set headers); the
Redis pubsub is behind the `_get_pubsub` seam so tests inject a fake that
yields queued messages, then None (which must produce `: ping` heartbeats).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.api.auth import create_access_token
from dealbot.db.models import User, Watchlist


class FakePubSub:
    def __init__(self, messages: list[str]):
        self.messages = list(messages)
        self.subscribed_channel: str | None = None
        self.closed = False

    async def subscribe(self, channel: str):
        self.subscribed_channel = channel

    async def get_message(self, ignore_subscribe_messages: bool = True,
                          timeout: float | None = None):
        if self.messages:
            return {"type": "message", "data": self.messages.pop(0).encode()}
        # Real redis blocks up to `timeout` — suspend so the stream generator
        # has a cancellation point instead of spinning the loop.
        import asyncio
        await asyncio.sleep(0.01)
        return None  # timeout → heartbeat

    async def unsubscribe(self, channel: str):
        pass

    async def aclose(self):
        self.closed = True


@pytest.fixture()
def stream_rig(client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.stream.get_async_session", _test_session)

    fake = FakePubSub(['{"type":"hunt.started","hunt_id":7}',
                      '{"type":"hunt.finished","hunt_id":7}'])
    monkeypatch.setattr("dealbot.api.routes.stream._get_pubsub", lambda wl_id: fake)
    return client, factory, fake


async def _seed(factory) -> tuple[int, int]:
    """Watchlist 1 owned by user 1, watchlist 2 owned by user 2."""
    async with factory() as s:
        u1 = User(id=1, email="one@t.com", hashed_password="x")
        u2 = User(id=2, email="two@t.com", hashed_password="x")
        s.add_all([u1, u2])
        await s.flush()
        wl1 = Watchlist(user_id=1, name="mine")
        wl2 = Watchlist(user_id=2, name="theirs")
        s.add_all([wl1, wl2])
        await s.flush()
        ids = (wl1.id, wl2.id)
        await s.commit()
        return ids


@pytest.mark.asyncio
async def test_stream_forwards_messages_then_heartbeats(stream_rig):
    """Drives the route's generator directly: TestClient cannot tear down an
    infinite StreamingResponse, and the generator IS the streaming logic."""
    _client, factory, fake = stream_rig
    wl1, _ = await _seed(factory)
    token = create_access_token(1)

    from dealbot.api.routes.stream import stream_watchlist_events

    resp = await stream_watchlist_events(wl1, token=token)
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"

    gen = resp.body_iterator
    chunks = [await gen.__anext__() for _ in range(3)]
    await gen.aclose()

    assert chunks == [
        'data: {"type":"hunt.started","hunt_id":7}\n\n',
        'data: {"type":"hunt.finished","hunt_id":7}\n\n',
        ": ping\n\n",
    ]
    assert fake.subscribed_channel == f"events:watchlist:{wl1}"
    assert fake.closed is True  # aclose ran the finally-block cleanup


@pytest.mark.asyncio
async def test_bad_token_401(stream_rig):
    client, factory, _ = stream_rig
    wl1, _ = await _seed(factory)
    resp = client.get(f"/stream/watchlists/{wl1}?token=not-a-jwt")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_missing_token_401(stream_rig):
    client, factory, _ = stream_rig
    wl1, _ = await _seed(factory)
    assert client.get(f"/stream/watchlists/{wl1}").status_code in (401, 422)


@pytest.mark.asyncio
async def test_foreign_watchlist_404(stream_rig):
    client, factory, _ = stream_rig
    _, wl2 = await _seed(factory)
    token = create_access_token(1)
    resp = client.get(f"/stream/watchlists/{wl2}?token={token}")
    assert resp.status_code == 404
