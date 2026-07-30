"""Per-event-loop caching of async singletons.

Celery runs each task under a fresh asyncio.run(); anything cached at module
level with loop affinity (SQLAlchemy async engine, redis clients) poisons
every task after the first ("attached to a different loop" — caught live in
the first real end-to-end hunt). These tests simulate the two-loop worker
pattern directly.
"""

from __future__ import annotations

import asyncio

from sqlalchemy import text


def test_engine_is_fresh_per_loop(monkeypatch):
    import dealbot.db.database as db

    monkeypatch.setattr(db, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    db._cache.update(loop=None, engine=None, sessionmaker=None)

    async def use_session():
        async with db.get_async_session() as session:
            await session.execute(text("SELECT 1"))
        return db._get_engine()

    engine_a = asyncio.run(use_session())
    engine_b = asyncio.run(use_session())  # crashed with a shared engine
    assert engine_a is not engine_b


def test_engine_stable_within_one_loop(monkeypatch):
    import dealbot.db.database as db

    monkeypatch.setattr(db, "DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    db._cache.update(loop=None, engine=None, sessionmaker=None)

    async def two_uses():
        first = db._get_engine()
        async with db.get_async_session() as session:
            await session.execute(text("SELECT 1"))
        return first, db._get_engine()

    first, second = asyncio.run(two_uses())
    assert first is second


def test_publisher_and_governor_fresh_per_loop():
    import dealbot.worker.governor as gov_mod
    import dealbot.worker.tasks as tasks_mod

    tasks_mod._publisher_cache.update(loop=None, publisher=None)
    gov_mod._governor_cache.update(loop=None, governor=None)

    async def grab():
        return tasks_mod._get_publisher(), gov_mod.build_governor()

    pub_a, gov_a = asyncio.run(grab())
    pub_b, gov_b = asyncio.run(grab())
    assert pub_a is not pub_b
    assert gov_a is not gov_b

