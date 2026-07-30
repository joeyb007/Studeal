"""Tests for the hunts listing API — Mission Control's load-state source."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Hunt, User, Watchlist

NOW = datetime.now(timezone.utc)


@pytest.fixture()
def hunts_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.hunts.get_async_session", _test_session)
    return authed_client, factory


async def _seed(factory) -> dict:
    """User 1 (authed fake): watchlist with 3 hunts (running/succeeded/failed);
    user 2: one watchlist with one hunt."""
    async with factory() as s:
        me = User(id=1, email="test@example.com", hashed_password="x")
        other = User(id=2, email="other@t.com", hashed_password="x")
        s.add_all([me, other])
        await s.flush()
        wl_me = Watchlist(user_id=1, name="Aeron watch")
        wl_other = Watchlist(user_id=2, name="Other watch")
        s.add_all([wl_me, wl_other])
        await s.flush()
        hunts = [
            Hunt(watchlist_id=wl_me.id, status="succeeded",
                 started_at=NOW - timedelta(hours=3), finished_at=NOW - timedelta(hours=3),
                 offer_count=40, persisted_count=30, new_listing_count=5),
            Hunt(watchlist_id=wl_me.id, status="failed",
                 started_at=NOW - timedelta(hours=2), finished_at=NOW - timedelta(hours=2),
                 error="browser died"),
            Hunt(watchlist_id=wl_me.id, status="running",
                 started_at=NOW - timedelta(minutes=1)),
            Hunt(watchlist_id=wl_other.id, status="running",
                 started_at=NOW - timedelta(minutes=5)),
        ]
        s.add_all(hunts)
        await s.flush()
        ids = {
            "wl_me": wl_me.id, "wl_other": wl_other.id,
            "succeeded": hunts[0].id, "failed": hunts[1].id,
            "running": hunts[2].id, "theirs": hunts[3].id,
        }
        await s.commit()
        return ids


@pytest.mark.asyncio
async def test_lists_own_hunts_newest_first(hunts_client):
    client, factory = hunts_client
    ids = await _seed(factory)
    resp = client.get("/hunts")
    assert resp.status_code == 200
    hunts = resp.json()["hunts"]
    assert [h["id"] for h in hunts] == [ids["running"], ids["failed"], ids["succeeded"]]
    assert all(h["watchlist_name"] == "Aeron watch" for h in hunts)
    top = hunts[0]
    assert top["status"] == "running" and top["finished_at"] is None
    assert hunts[2]["offer_count"] == 40 and hunts[2]["new_listing_count"] == 5
    assert hunts[1]["error"] == "browser died"


@pytest.mark.asyncio
async def test_status_filter(hunts_client):
    client, factory = hunts_client
    ids = await _seed(factory)
    hunts = client.get("/hunts", params={"status": "running"}).json()["hunts"]
    assert [h["id"] for h in hunts] == [ids["running"]]


@pytest.mark.asyncio
async def test_limit(hunts_client):
    client, factory = hunts_client
    await _seed(factory)
    hunts = client.get("/hunts", params={"limit": 2}).json()["hunts"]
    assert len(hunts) == 2


@pytest.mark.asyncio
async def test_watchlist_hunts(hunts_client):
    client, factory = hunts_client
    ids = await _seed(factory)
    resp = client.get(f"/watchlists/{ids['wl_me']}/hunts")
    assert resp.status_code == 200
    assert len(resp.json()["hunts"]) == 3


@pytest.mark.asyncio
async def test_foreign_watchlist_hunts_404(hunts_client):
    client, factory = hunts_client
    ids = await _seed(factory)
    assert client.get(f"/watchlists/{ids['wl_other']}/hunts").status_code == 404
