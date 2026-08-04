"""Watchlist playbook: comps math, sanitizer, generation write path."""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.playbook import price_band, sanitize
from dealbot.db.models import Base, User, Watchlist


def test_price_band_percentiles():
    prices = [100.0, 150.0, 200.0, 250.0, 300.0, 350.0, 400.0, 450.0]
    p25, p50, p75 = price_band(prices)
    assert p25 < p50 < p75
    assert p50 == pytest.approx(275.0)


def test_price_band_needs_five_comps():
    assert price_band([100.0, 200.0, 300.0, 400.0]) is None


def test_sanitize_strips_em_dashes():
    assert sanitize("check the cylinder — it fails first") == "check the cylinder · it fails first"
    assert sanitize("mid—word") == "mid-word"
    assert sanitize("clean text") == "clean text"


class _MockLLM:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls += 1
        content = self._content

        class R:  # noqa: N801
            pass

        r = R()
        r.content = content
        return r


@pytest.fixture()
async def db(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _s() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.agents.playbook.get_async_session", _s)
    yield factory
    await engine.dispose()


async def _seed_watchlist(factory, email: str, name: str, context: str) -> int:
    async with factory() as session:
        user = User(email=email, hashed_password="x")
        session.add(user)
        await session.flush()
        wl = Watchlist(user_id=user.id, name=name, context=context)
        session.add(wl)
        await session.commit()
        return wl.id


@pytest.mark.asyncio
async def test_generate_writes_sanitized_playbook(db, monkeypatch):
    from dealbot.agents import playbook as pb

    factory = db
    wl_id = await _seed_watchlist(
        factory, "t@example.com", "Desk chairs",
        '{"product_query": "ergonomic office chair", "max_budget": 400.0}',
    )

    monkeypatch.setattr(pb, "_get_llm", lambda: _MockLLM("Check the tilt — worth it."))
    assert await pb.generate_playbook(wl_id) is True

    async with factory() as session:
        row = await session.get(Watchlist, wl_id)
    assert row.playbook == "Check the tilt · worth it."
    assert row.playbook_updated_at is not None


@pytest.mark.asyncio
async def test_generate_survives_llm_failure(db, monkeypatch):
    from dealbot.agents import playbook as pb

    factory = db
    wl_id = await _seed_watchlist(
        factory, "t2@example.com", "Bikes", '{"product_query": "bike"}',
    )

    class _Boom:
        async def complete(self, *a, **k):
            raise RuntimeError("llm down")

    monkeypatch.setattr(pb, "_get_llm", lambda: _Boom())
    assert await pb.generate_playbook(wl_id) is False
    async with factory() as session:
        row = await session.get(Watchlist, wl_id)
    assert row.playbook is None


def test_task_delegates_to_generator(monkeypatch):
    from dealbot.worker import tasks as t

    called: list[int] = []

    async def _fake(wl_id: int) -> bool:
        called.append(wl_id)
        return True

    monkeypatch.setattr("dealbot.agents.playbook.generate_playbook", _fake)
    result = t.generate_playbook_task.run(41)
    assert result == {"ok": True}
    assert called == [41]


@pytest.mark.asyncio
async def test_playbook_round_trips_through_api(authed_client, db_factory):
    async with db_factory() as session:
        user = User(email="test@example.com", hashed_password="x")
        user.id = 1
        session.add(user)
        await session.flush()
        wl = Watchlist(
            user_id=1, name="Desk chairs",
            context='{"product_query": "ergonomic office chair"}',
            playbook="What to check\nCheck the tilt.",
        )
        session.add(wl)
        await session.commit()

    resp = authed_client.get("/watchlists")
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["playbook"] == "What to check\nCheck the tilt."
