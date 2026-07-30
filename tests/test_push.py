"""Tests for web push: subscription routes + VAPID sender.

Routes ride the authed_client fixture (fake user id=1); the sender is tested
with pywebpush.webpush monkeypatched — 404/410 from a push service means the
subscription is expired and its row must be deleted.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from pywebpush import WebPushException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, PushSubscription, User

SUB_BODY = {
    "endpoint": "https://push.example.com/e1",
    "keys": {"p256dh": "key-p256", "auth": "key-auth"},
}


@pytest.fixture()
def push_client(authed_client, db_factory, monkeypatch):
    """authed_client with the push routes' session patched to the same rig."""
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.push.get_async_session", _test_session)
    return authed_client, factory


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_vapid_public_key_503_without_env(push_client, monkeypatch):
    client, _ = push_client
    monkeypatch.delenv("VAPID_PUBLIC_KEY", raising=False)
    assert client.get("/push/vapid-public-key").status_code == 503


def test_vapid_public_key_returned(push_client, monkeypatch):
    client, _ = push_client
    monkeypatch.setenv("VAPID_PUBLIC_KEY", "BPubKey")
    resp = client.get("/push/vapid-public-key")
    assert resp.status_code == 200
    assert resp.json() == {"key": "BPubKey"}


@pytest.mark.asyncio
async def test_subscribe_creates_row(push_client):
    client, factory = push_client
    resp = client.post("/push/subscribe", json=SUB_BODY)
    assert resp.status_code == 201
    async with factory() as s:
        row = (await s.execute(select(PushSubscription))).scalar_one()
        assert row.user_id == 1
        assert row.endpoint == SUB_BODY["endpoint"]
        assert row.p256dh == "key-p256" and row.auth == "key-auth"


@pytest.mark.asyncio
async def test_subscribe_same_endpoint_upserts(push_client):
    client, factory = push_client
    client.post("/push/subscribe", json=SUB_BODY)
    updated = {**SUB_BODY, "keys": {"p256dh": "new-p256", "auth": "new-auth"}}
    resp = client.post("/push/subscribe", json=updated)
    assert resp.status_code == 201
    async with factory() as s:
        rows = (await s.execute(select(PushSubscription))).scalars().all()
        assert len(rows) == 1
        assert rows[0].p256dh == "new-p256"


@pytest.mark.asyncio
async def test_unsubscribe_deletes_own_row(push_client):
    client, factory = push_client
    client.post("/push/subscribe", json=SUB_BODY)
    resp = client.request("DELETE", "/push/subscribe",
                          json={"endpoint": SUB_BODY["endpoint"]})
    assert resp.status_code == 204
    async with factory() as s:
        assert (await s.execute(select(PushSubscription))).scalars().all() == []


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

@pytest.fixture()
async def sender_rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as s:
            yield s

    import dealbot.notifications.push as push_mod

    monkeypatch.setattr(push_mod, "get_async_session", _session)
    monkeypatch.setenv("VAPID_PRIVATE_KEY", "priv")
    monkeypatch.setenv("VAPID_SUBJECT", "mailto:test@studeal.site")

    async with factory() as s:
        user = User(email="p@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        for i in range(2):
            s.add(PushSubscription(
                user_id=user.id, endpoint=f"https://push.example.com/{i}",
                p256dh="p", auth="a",
            ))
        user_id = user.id
        await s.commit()

    yield factory, push_mod, user_id, monkeypatch
    await engine.dispose()


@pytest.mark.asyncio
async def test_send_push_to_all_subscriptions(sender_rig):
    factory, push_mod, user_id, monkeypatch = sender_rig
    calls = []
    monkeypatch.setattr(push_mod, "webpush", lambda **kw: calls.append(kw))

    sent = await push_mod.send_push_to_user(
        user_id, push_mod.PushPayload(title="t", body="b", url="https://k.ca/1"),
    )
    assert sent == 2
    assert len(calls) == 2
    endpoints = {c["subscription_info"]["endpoint"] for c in calls}
    assert endpoints == {"https://push.example.com/0", "https://push.example.com/1"}
    assert all(c["vapid_private_key"] == "priv" for c in calls)


@pytest.mark.asyncio
async def test_expired_subscription_deleted(sender_rig):
    factory, push_mod, user_id, monkeypatch = sender_rig

    class FakeResponse:
        status_code = 410

    def exploding_webpush(**kw):
        if kw["subscription_info"]["endpoint"].endswith("/0"):
            raise WebPushException("gone", response=FakeResponse())

    monkeypatch.setattr(push_mod, "webpush", exploding_webpush)

    sent = await push_mod.send_push_to_user(
        user_id, push_mod.PushPayload(title="t", body="b", url="https://k.ca/1"),
    )
    assert sent == 1  # only the healthy subscription counts
    async with factory() as s:
        rows = (await s.execute(select(PushSubscription))).scalars().all()
        assert [r.endpoint for r in rows] == ["https://push.example.com/1"]


@pytest.mark.asyncio
async def test_missing_vapid_env_returns_zero(sender_rig):
    _factory, push_mod, user_id, monkeypatch = sender_rig
    monkeypatch.delenv("VAPID_PRIVATE_KEY")
    sent = await push_mod.send_push_to_user(
        user_id, push_mod.PushPayload(title="t", body="b", url="u"),
    )
    assert sent == 0
