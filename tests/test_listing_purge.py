"""Hard purge at LISTING_PURGE_DAYS. Deliberately far behind the stale window:
reads stop serving a listing at 7 days, but its row (and price history) survive
to 90 for the future price-intelligence layer.

Pinned behavior: the FK cascade means a purged listing deletes its
listing_alerts and hunt_listings rows. 90-day-old alert history dying with its
listing is accepted, not accidental.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Listing
from dealbot.lifecycle import LISTING_PURGE_DAYS

NOW = datetime.now(timezone.utc)


@pytest.fixture()
async def factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    await engine.dispose()


def _listing(n: int, *, age_days: int) -> Listing:
    return Listing(
        canonical_url=f"c{n}", raw_url=f"https://m.test/{n}",
        marketplace="kijiji", title=f"Item {n}", price=100.0,
        currency="CAD", condition="used",
        first_seen_at=NOW - timedelta(days=age_days),
        last_seen_at=NOW - timedelta(days=age_days),
    )


@pytest.mark.asyncio
async def test_purges_only_past_the_cutoff(factory, monkeypatch):
    import dealbot.worker.celery_app as celery_mod

    @asynccontextmanager
    async def _session():
        async with factory() as s:
            yield s

    monkeypatch.setattr("dealbot.db.database.get_async_session", _session)

    async with factory() as s:
        s.add_all([
            _listing(1, age_days=LISTING_PURGE_DAYS + 10),   # purged
            _listing(2, age_days=LISTING_PURGE_DAYS - 10),   # stale but kept
            _listing(3, age_days=1),                          # fresh
        ])
        await s.commit()

    result = await celery_mod._run_listing_purge()
    assert result["deleted"] == 1

    async with factory() as s:
        titles = {l.title for l in (await s.execute(select(Listing))).scalars()}
    assert titles == {"Item 2", "Item 3"}, (
        "stale-but-unpurged rows must survive — they are price history"
    )


def test_beat_schedule_has_the_entry():
    from dealbot.worker.celery_app import app

    entry = app.conf.beat_schedule["cleanup-old-listings"]
    assert entry["task"] == "dealbot.worker.celery_app.cleanup_old_listings"
