from __future__ import annotations

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Hunt, HuntListing, Listing, ListingAlert, PushSubscription, User, Watchlist


@pytest.fixture()
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s
    await engine.dispose()


async def _seed_user_watchlist(session) -> Watchlist:
    user = User(email="t@t.com", hashed_password="x")
    session.add(user)
    await session.flush()
    wl = Watchlist(user_id=user.id, name="aeron")
    session.add(wl)
    await session.flush()
    return wl


async def test_hunt_lifecycle_rows(session):
    wl = await _seed_user_watchlist(session)
    hunt = Hunt(watchlist_id=wl.id)
    session.add(hunt)
    await session.flush()
    assert hunt.status == "running"
    assert hunt.offer_count == 0
    assert hunt.finished_at is None


async def test_hunt_listing_association(session):
    wl = await _seed_user_watchlist(session)
    hunt = Hunt(watchlist_id=wl.id)
    listing = Listing(canonical_url="https://k.ca/1", raw_url="https://k.ca/1?x=1",
                      marketplace="kijiji", title="Aeron", price=400.0)
    session.add_all([hunt, listing])
    await session.flush()
    session.add(HuntListing(hunt_id=hunt.id, listing_id=listing.id))
    await session.flush()
    row = await session.get(HuntListing, (hunt.id, listing.id))
    assert row is not None and row.was_new_for_watchlist is False


async def test_watchlist_scheduling_defaults(session):
    wl = await _seed_user_watchlist(session)
    assert wl.hunting_enabled is True
    assert wl.hunt_frequency_minutes is None
    assert wl.last_hunt_at is None


async def test_alert_and_push_rows(session):
    wl = await _seed_user_watchlist(session)
    hunt = Hunt(watchlist_id=wl.id)
    listing = Listing(canonical_url="https://k.ca/2", raw_url="https://k.ca/2",
                      marketplace="kijiji", title="Aeron", price=350.0)
    session.add_all([hunt, listing])
    await session.flush()
    alert = ListingAlert(user_id=wl.user_id, watchlist_id=wl.id,
                         listing_id=listing.id, hunt_id=hunt.id, score=0.91)
    sub = PushSubscription(user_id=wl.user_id, endpoint="https://push/e1", p256dh="k", auth="a")
    session.add_all([alert, sub])
    await session.flush()
    assert alert.channels == "feed" and alert.reason is None and alert.read_at is None
    assert sub.id is not None
