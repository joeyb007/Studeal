"""Tests for persist_offers → listings table.

Uses an in-memory SQLite database. Note: the production DB is Postgres, and
`persist_offers` uses `pg_insert` for ON CONFLICT semantics — SQLite supports
the same syntax via SQLAlchemy's dialect emulation for testing.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.workers.extractor import Offer
from dealbot.db.models import Base, Listing


@pytest.fixture()
async def db_setup(monkeypatch):
    """In-memory SQLite; patch `get_async_session` used inside persist_offers."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr(
        "dealbot.persistence.listings.get_async_session", _test_session,
    )
    yield factory
    await engine.dispose()


def _offer(url: str, marketplace: str = "kijiji", **kw) -> Offer:
    defaults = dict(
        title="Aeron chair", price=500.0, currency="CAD",
        url=url, marketplace=marketplace,
    )
    defaults.update(kw)
    return Offer(**defaults)


@pytest.mark.asyncio
async def test_persists_new_offers(db_setup):
    from dealbot.persistence.listings import persist_offers
    offers = [
        _offer("https://www.kijiji.ca/v-office/aeron/1"),
        _offer("https://www.kijiji.ca/v-office/aeron/2"),
    ]
    written = await persist_offers(offers)
    assert written == 2

    factory = db_setup
    async with factory() as session:
        rows = (await session.execute(select(Listing))).scalars().all()
        assert len(rows) == 2
        assert {r.canonical_url for r in rows} == {
            "https://www.kijiji.ca/v-office/aeron/1",
            "https://www.kijiji.ca/v-office/aeron/2",
        }


@pytest.mark.asyncio
async def test_dedupes_url_variants_via_canonicalization(db_setup):
    """Two Offers with the same canonical URL → single row after upsert."""
    from dealbot.persistence.listings import persist_offers
    offers = [
        _offer("https://www.kijiji.ca/v-office/aeron/1?ref=share"),
        _offer("https://www.kijiji.ca/v-office/aeron/1#reviews"),
    ]
    await persist_offers(offers)

    factory = db_setup
    async with factory() as session:
        rows = (await session.execute(select(Listing))).scalars().all()
    assert len(rows) == 1
    assert rows[0].canonical_url == "https://www.kijiji.ca/v-office/aeron/1"


@pytest.mark.asyncio
async def test_second_write_updates_price_and_last_seen(db_setup):
    """Re-writing the same canonical URL updates fields and bumps last_seen_at."""
    from dealbot.persistence.listings import persist_offers
    url = "https://www.kijiji.ca/v-office/aeron/42"

    await persist_offers([_offer(url, price=600.0, title="Aeron old")])
    factory = db_setup
    async with factory() as session:
        first_row = (await session.execute(select(Listing))).scalar_one()
        first_seen = first_row.first_seen_at

    await persist_offers([_offer(url, price=450.0, title="Aeron reduced")])
    async with factory() as session:
        second_row = (await session.execute(select(Listing))).scalar_one()

    assert second_row.price == 450.0
    assert second_row.title == "Aeron reduced"
    assert second_row.first_seen_at == first_seen  # preserved
    assert second_row.last_seen_at >= first_seen   # bumped forward


@pytest.mark.asyncio
async def test_empty_offers_returns_zero(db_setup):
    from dealbot.persistence.listings import persist_offers
    assert await persist_offers([]) == 0
