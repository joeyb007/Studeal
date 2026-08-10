"""Phase 2 trust/quality seams (spec 2026-08-10): the under-market read,
the quality-bar predicate, and the bounded demotion reorder."""

from dealbot.recsys.market_stats import under_market
from dealbot.worker.inspections import fails_quality_bar

PRICES = [200.0, 220.0, 240.0, 260.0, 280.0, 300.0]  # median 250


# ---- under_market ---------------------------------------------------------

def test_under_market_flags_deep_discount():
    read = under_market(100.0, PRICES, "Apple AirPods Max silver")
    assert read == {"pct": 60}


def test_under_market_ignores_normal_prices():
    assert under_market(240.0, PRICES, "Apple AirPods Max silver") is None


def test_under_market_honest_disclosure_carve_out():
    assert under_market(100.0, PRICES, "AirPods Max FOR PARTS cracked hinge") is None


def test_under_market_needs_enough_comps():
    assert under_market(50.0, [200.0, 210.0], "AirPods Max") is None


# ---- fails_quality_bar ----------------------------------------------------

def _flags(**kw):
    base = {"photos_real": True, "condition_grade": "good", "legit_level": "fine", "legit_reason": ""}
    base.update(kw)
    return base


def test_no_bar_never_fails():
    assert not fails_quality_bar(None, _flags(condition_grade="worn"))
    assert not fails_quality_bar("any", _flags(condition_grade="worn"))


def test_missing_flags_never_fail():
    assert not fails_quality_bar("pristine", None)


def test_stock_photos_fail_any_set_bar():
    assert fails_quality_bar("wear_ok", _flags(photos_real=False))


def test_unknown_photos_pass():
    assert not fails_quality_bar("pristine", _flags(photos_real=None))


def test_grade_ladder():
    assert fails_quality_bar("pristine", _flags(condition_grade="fair"))
    assert not fails_quality_bar("good", _flags(condition_grade="fair"))
    assert fails_quality_bar("good", _flags(condition_grade="worn"))
    assert not fails_quality_bar("wear_ok", _flags(condition_grade="worn"))


def test_unknown_grade_passes_every_bar():
    assert not fails_quality_bar("pristine", _flags(condition_grade="unknown"))


# ---- enforce_quality_bar reorder -----------------------------------------

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone

import pytest

from dealbot.db.models import Listing, ListingInspection, User, Watchlist, WatchlistRanking


def _listing(canonical: str) -> Listing:
    now = datetime.now(timezone.utc)
    return Listing(
        canonical_url=canonical, raw_url=canonical, marketplace="kijiji",
        title=f"item {canonical} with a long enough title", price=100.0,
        currency="CAD", condition="used", first_seen_at=now, last_seen_at=now,
    )


@pytest.mark.asyncio
async def test_enforce_quality_bar_demotes_and_promotes(db_factory, monkeypatch):
    @asynccontextmanager
    async def _s():
        async with db_factory() as session:
            yield session

    monkeypatch.setattr("dealbot.worker.inspections.get_async_session", _s)

    inspected: list[int] = []

    async def _fake_inspect(listing_id: int):
        inspected.append(listing_id)
        return {"status": "ok"}

    monkeypatch.setattr(
        "dealbot.agents.inspector.get_or_create_inspection", _fake_inspect
    )

    async with db_factory() as session:
        user = User(email="q@example.com", hashed_password="x")
        session.add(user)
        await session.flush()
        wl = Watchlist(
            user_id=user.id, name="AirPods",
            context=json.dumps({"product_query": "airpods max", "quality_bar": "good"}),
        )
        session.add(wl)
        await session.flush()

        listings = [_listing(f"q-{i}") for i in range(7)]
        session.add_all(listings)
        await session.flush()

        now = datetime.now(timezone.utc)
        for pos, listing in enumerate(listings):
            session.add(WatchlistRanking(
                watchlist_id=wl.id, listing_id=listing.id,
                score=0.9 - pos * 0.05, position=pos, computed_at=now,
            ))
        # Pick at position 1 is worn (fails "good"); position 3 has stock photos.
        session.add(ListingInspection(
            listing_id=listings[1].id, status="ok",
            flags={"photos_real": True, "condition_grade": "worn"},
        ))
        session.add(ListingInspection(
            listing_id=listings[3].id, status="ok",
            flags={"photos_real": False, "condition_grade": "good"},
        ))
        await session.commit()
        wl_id = wl.id
        ids = [l.id for l in listings]

    from dealbot.worker.inspections import enforce_quality_bar

    demoted = await enforce_quality_bar(wl_id)
    assert demoted == 2

    async with db_factory() as session:
        from sqlalchemy import select
        rows = (await session.execute(
            select(WatchlistRanking)
            .where(WatchlistRanking.watchlist_id == wl_id)
            .order_by(WatchlistRanking.position)
        )).scalars().all()
    ordered = [r.listing_id for r in rows]
    # Failing picks sank to the back of the curated block, keepers closed up.
    assert ordered == [ids[0], ids[2], ids[4], ids[5], ids[6], ids[1], ids[3]]
    # Every uninspected pick in the NEW top 5 got a read (keepers included;
    # in production those are cache hits), bounded at 5, none twice.
    assert sorted(inspected) == [ids[0], ids[2], ids[4], ids[5], ids[6]]


@pytest.mark.asyncio
async def test_enforce_quality_bar_noop_without_bar(db_factory, monkeypatch):
    @asynccontextmanager
    async def _s():
        async with db_factory() as session:
            yield session

    monkeypatch.setattr("dealbot.worker.inspections.get_async_session", _s)

    async with db_factory() as session:
        user = User(email="q2@example.com", hashed_password="x")
        session.add(user)
        await session.flush()
        wl = Watchlist(
            user_id=user.id, name="Chairs",
            context=json.dumps({"product_query": "chair"}),
        )
        session.add(wl)
        await session.commit()
        wl_id = wl.id

    from dealbot.worker.inspections import enforce_quality_bar

    assert await enforce_quality_bar(wl_id) == 0
