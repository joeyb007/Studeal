"""Tests for the alerts feed API — the in-app alert channel."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.db.models import Hunt, Listing, ListingAlert, User, Watchlist


@pytest.fixture()
def alerts_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.alerts.get_async_session", _test_session)
    return authed_client, factory


async def _seed(factory) -> dict:
    """User 1 (the authed fake) gets two alerts (one read); user 2 gets one."""
    t0 = datetime.now(timezone.utc)
    async with factory() as s:
        me = User(id=1, email="test@example.com", hashed_password="x")
        other = User(id=2, email="other@t.com", hashed_password="x")
        s.add_all([me, other])
        await s.flush()
        wl_me = Watchlist(user_id=1, name="Aeron watch")
        wl_other = Watchlist(user_id=2, name="Other watch")
        s.add_all([wl_me, wl_other])
        await s.flush()
        hunt = Hunt(watchlist_id=wl_me.id)
        hunt_other = Hunt(watchlist_id=wl_other.id)
        s.add_all([hunt, hunt_other])
        listings = [
            Listing(canonical_url=f"c{i}", raw_url=f"https://k.ca/{i}",
                    marketplace="kijiji", title=f"Aeron {i}", price=100.0 + i,
                    currency="CAD", image_url=f"https://img/{i}.jpg")
            for i in range(3)
        ]
        s.add_all(listings)
        await s.flush()
        alerts = [
            ListingAlert(user_id=1, watchlist_id=wl_me.id, listing_id=listings[0].id,
                         hunt_id=hunt.id, score=0.9, created_at=t0 - timedelta(hours=1)),
            ListingAlert(user_id=1, watchlist_id=wl_me.id, listing_id=listings[1].id,
                         hunt_id=hunt.id, score=0.8, created_at=t0,
                         read_at=t0, reason="matches your budget"),
            ListingAlert(user_id=2, watchlist_id=wl_other.id, listing_id=listings[2].id,
                         hunt_id=hunt_other.id, score=0.7, created_at=t0),
        ]
        s.add_all(alerts)
        await s.flush()
        ids = {"mine_unread": alerts[0].id, "mine_read": alerts[1].id,
               "theirs": alerts[2].id}
        await s.commit()
        return ids


@pytest.mark.asyncio
async def test_feed_returns_own_newest_first(alerts_client):
    client, factory = alerts_client
    ids = await _seed(factory)
    resp = client.get("/alerts")
    assert resp.status_code == 200
    data = resp.json()
    assert [a["id"] for a in data["alerts"]] == [ids["mine_read"], ids["mine_unread"]]
    assert data["unread_count"] == 1
    first = data["alerts"][0]
    assert first["watchlist_name"] == "Aeron watch"
    assert first["title"] == "Aeron 1"
    assert first["url"] == "https://k.ca/1"
    assert first["reason"] == "matches your budget"


@pytest.mark.asyncio
async def test_unread_only_filter(alerts_client):
    client, factory = alerts_client
    ids = await _seed(factory)
    resp = client.get("/alerts", params={"unread_only": "true"})
    data = resp.json()
    assert [a["id"] for a in data["alerts"]] == [ids["mine_unread"]]
    assert data["unread_count"] == 1


@pytest.mark.asyncio
async def test_mark_read(alerts_client):
    client, factory = alerts_client
    ids = await _seed(factory)
    assert client.post(f"/alerts/{ids['mine_unread']}/read").status_code == 204
    async with factory() as s:
        row = await s.get(ListingAlert, ids["mine_unread"])
        assert row.read_at is not None


@pytest.mark.asyncio
async def test_mark_read_foreign_alert_404(alerts_client):
    client, factory = alerts_client
    ids = await _seed(factory)
    assert client.post(f"/alerts/{ids['theirs']}/read").status_code == 404
    async with factory() as s:
        row = await s.get(ListingAlert, ids["theirs"])
        assert row.read_at is None


@pytest.mark.asyncio
async def test_read_all(alerts_client):
    client, factory = alerts_client
    await _seed(factory)
    resp = client.post("/alerts/read-all")
    assert resp.status_code == 200
    assert resp.json() == {"marked": 1}  # only my unread one
    async with factory() as s:
        mine = (await s.execute(
            select(ListingAlert).where(ListingAlert.user_id == 1)
        )).scalars().all()
        assert all(a.read_at is not None for a in mine)
