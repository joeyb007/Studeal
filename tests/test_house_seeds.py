"""House watchlists that keep the shared pool warm.

Ordinary watchlist rows owned by a system user — the existing scheduler hunts
them on cadence with no special-casing. Idempotent so it can run on every deploy.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, User, Watchlist


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_seeds_fifteen_categories(session):
    from scripts.seed_house_watchlists import HOUSE_CATEGORIES, seed_house_watchlists

    assert len(HOUSE_CATEGORIES) == 15
    result = await seed_house_watchlists(session)
    assert result["created"] == 15

    rows = (await session.execute(select(Watchlist))).scalars().all()
    assert len(rows) == 15
    assert all(w.hunting_enabled for w in rows)
    assert all(w.hunt_frequency_minutes == 1440 for w in rows)
    assert all(w.context for w in rows), "no context → the scheduler skips it"


@pytest.mark.asyncio
async def test_is_idempotent(session):
    from scripts.seed_house_watchlists import seed_house_watchlists

    await seed_house_watchlists(session)
    second = await seed_house_watchlists(session)
    assert second["created"] == 0
    assert second["existing"] == 15
    rows = (await session.execute(select(Watchlist))).scalars().all()
    assert len(rows) == 15


@pytest.mark.asyncio
async def test_system_user_is_pro(session):
    from scripts.seed_house_watchlists import HOUSE_EMAIL, seed_house_watchlists

    await seed_house_watchlists(session)
    user = (await session.execute(
        select(User).where(User.email == HOUSE_EMAIL)
    )).scalar_one()
    assert user.is_pro is True, "house seeds must bypass free-tier caps"
