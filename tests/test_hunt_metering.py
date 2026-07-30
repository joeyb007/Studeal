"""Tests for manual hunt-trigger metering.

Manual hunts share the tier cadence budget with scheduled ones: inside the
cadence window POST /{id}/hunt answers 429; a dispatch stamps last_hunt_at so
the scheduler and manual triggers can't double-spend.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from dealbot.api.auth import get_current_user
from dealbot.api.main import app
from dealbot.db.models import User, Watchlist

CONTEXT = '{"product_query": "aeron"}'
NOW = datetime.now(timezone.utc)


@pytest.fixture()
def metering_client(authed_client, db_factory, monkeypatch):
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    # authed_client already patches routes.watchlists' session; stub the
    # Celery dispatch so no broker is needed.
    dispatched: list[int] = []

    class FakeTask:
        @staticmethod
        def delay(watchlist_id: int):
            dispatched.append(watchlist_id)

    monkeypatch.setattr("dealbot.worker.tasks.research_for_agent", FakeTask)

    def set_pro(is_pro: bool) -> None:
        """The authed_client fixture's fake user is free-tier; pro tests
        re-override the dependency (cleared by the fixture's teardown)."""
        fake = User()
        fake.id = 1
        fake.email = "test@example.com"
        fake.is_pro = is_pro

        async def _fake_current_user():
            return fake

        app.dependency_overrides[get_current_user] = _fake_current_user

    return authed_client, factory, dispatched, set_pro


async def _seed(factory, *, is_pro: bool, last_hunt_at) -> int:
    async with factory() as s:
        user = User(id=1, email="test@example.com", hashed_password="x", is_pro=is_pro)
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=1, name="aeron", context=CONTEXT, last_hunt_at=last_hunt_at)
        s.add(wl)
        await s.flush()
        wl_id = wl.id
        await s.commit()
        return wl_id


@pytest.mark.asyncio
async def test_free_user_inside_cadence_gets_429(metering_client):
    client, factory, dispatched, set_pro = metering_client
    wl = await _seed(factory, is_pro=False, last_hunt_at=NOW - timedelta(hours=2))
    resp = client.post(f"/watchlists/{wl}/hunt")
    assert resp.status_code == 429
    assert "next hunt" in resp.json()["detail"].lower()
    assert dispatched == []


@pytest.mark.asyncio
async def test_free_user_outside_cadence_dispatches(metering_client):
    client, factory, dispatched, set_pro = metering_client
    wl = await _seed(factory, is_pro=False, last_hunt_at=NOW - timedelta(hours=25))
    resp = client.post(f"/watchlists/{wl}/hunt")
    assert resp.status_code == 202
    assert dispatched == [wl]
    async with factory() as s:
        row = await s.get(Watchlist, wl)
        got = row.last_hunt_at
        if got.tzinfo is None:
            got = got.replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc) - got).total_seconds() < 60  # stamped


@pytest.mark.asyncio
async def test_never_hunted_dispatches(metering_client):
    client, factory, dispatched, set_pro = metering_client
    wl = await _seed(factory, is_pro=False, last_hunt_at=None)
    assert client.post(f"/watchlists/{wl}/hunt").status_code == 202
    assert dispatched == [wl]


@pytest.mark.asyncio
async def test_pro_user_hourly_window(metering_client):
    client, factory, dispatched, set_pro = metering_client
    set_pro(True)
    wl = await _seed(factory, is_pro=True, last_hunt_at=NOW - timedelta(hours=2))
    assert client.post(f"/watchlists/{wl}/hunt").status_code == 202  # 2h > hourly cadence
    assert dispatched == [wl]


@pytest.mark.asyncio
async def test_pro_user_inside_hour_gets_429(metering_client):
    client, factory, dispatched, set_pro = metering_client
    set_pro(True)
    wl = await _seed(factory, is_pro=True, last_hunt_at=NOW - timedelta(minutes=10))
    assert client.post(f"/watchlists/{wl}/hunt").status_code == 429
    assert dispatched == []
