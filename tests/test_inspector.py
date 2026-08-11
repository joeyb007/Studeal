"""Deal Inspector Tier A: cache semantics, dead-listing branch, sanitize,
API round-trips with the browser+LLM seams mocked."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.inspector import ListingDetail, _sanitize_obj
from dealbot.db.models import Base, Listing, ListingInspection, User, Watchlist


def _listing(**kw) -> Listing:
    defaults = dict(
        canonical_url="https://www.kijiji.ca/v-chair/1", raw_url="https://www.kijiji.ca/v-chair/1?x=1",
        marketplace="kijiji", title="Aeron chair", price=300.0, currency="CAD",
        condition="used",
    )
    defaults.update(kw)
    return Listing(**defaults)


def test_sanitize_obj_recurses():
    report = {
        "summary": "solid — buy it",
        "red_flags": ["wear — visible"],
        "legitimacy": {"level": "fine", "reason": "priced right — normal"},
    }
    out = _sanitize_obj(report)
    assert out["summary"] == "solid · buy it"
    assert out["red_flags"] == ["wear · visible"]
    assert out["legitimacy"]["reason"] == "priced right · normal"


@pytest.fixture()
async def idb(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _s() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.agents.inspector.get_async_session", _s)
    yield factory
    await engine.dispose()


async def _seed_listing(factory, **kw) -> int:
    async with factory() as session:
        row = _listing(**kw)
        session.add(row)
        await session.commit()
        return row.id


@pytest.mark.asyncio
async def test_cache_hit_skips_browser(idb, monkeypatch):
    from dealbot.agents import inspector as insp

    factory = idb
    lid = await _seed_listing(factory)
    async with factory() as session:
        session.add(ListingInspection(
            listing_id=lid, status="ok",
            report=json.dumps({"summary": "cached take", "comps": []}),
            detail=ListingDetail().model_dump_json(),
            created_at=datetime.now(timezone.utc),
        ))
        await session.commit()

    async def _no_visit(listing):
        raise AssertionError("browser must not be touched on cache hit")

    monkeypatch.setattr(insp, "_visit", _no_visit)
    out = await insp.get_or_create_inspection(lid)
    assert out["cached"] is True
    assert out["status"] == "ok"
    assert out["report"]["summary"] == "cached take"


@pytest.mark.asyncio
async def test_dead_page_marks_sold_and_caches_gone(idb, monkeypatch):
    from dealbot.agents import inspector as insp

    factory = idb
    lid = await _seed_listing(factory)

    async def _fake_visit(listing):
        return ("This listing is no longer available", [b"jpg"], False)

    async def _fake_detail(text):
        return ListingDetail(gone=True)

    async def _no_comps(listing):
        return []

    monkeypatch.setattr(insp, "_visit", _fake_visit)
    monkeypatch.setattr(insp, "_extract_detail", _fake_detail)
    monkeypatch.setattr(insp, "_comps", _no_comps)

    out = await insp.get_or_create_inspection(lid)
    assert out["status"] == "listing_gone"

    async with factory() as session:
        listing = await session.get(Listing, lid)
        cache = await session.get(ListingInspection, lid)
    assert listing.sold_at is not None
    assert cache.status == "listing_gone"


@pytest.mark.asyncio
async def test_nav_failure_is_error_not_sold(idb, monkeypatch):
    from dealbot.agents import inspector as insp

    factory = idb
    lid = await _seed_listing(factory)

    async def _fail_visit(listing):
        return None

    async def _no_comps(listing):
        return []

    monkeypatch.setattr(insp, "_visit", _fail_visit)
    monkeypatch.setattr(insp, "_comps", _no_comps)

    out = await insp.get_or_create_inspection(lid)
    assert out["status"] == "error"

    async with factory() as session:
        listing = await session.get(Listing, lid)
        cache = await session.get(ListingInspection, lid)
    assert listing.sold_at is None      # nav failure is not death
    assert cache is None                # errors never cached


@pytest.mark.asyncio
async def test_ok_flow_writes_cache_with_comps(idb, monkeypatch):
    from dealbot.agents import inspector as insp

    factory = idb
    lid = await _seed_listing(factory)

    async def _fake_visit(listing):
        return ("normal listing page", [b"jpg1", b"jpg2"], False)

    async def _fake_detail(text):
        return ListingDetail(description="great chair")

    async def _fake_comps(listing):
        return [{"id": 9, "title": "Other chair", "price": 280.0, "marketplace": "kijiji"}]

    async def _fake_report(listing, detail, frames, comps):
        return {"summary": "solid", "comps": comps[:3]}

    monkeypatch.setattr(insp, "_visit", _fake_visit)
    monkeypatch.setattr(insp, "_extract_detail", _fake_detail)
    monkeypatch.setattr(insp, "_comps", _fake_comps)
    monkeypatch.setattr(insp, "_generate_report", _fake_report)

    out = await insp.get_or_create_inspection(lid)
    assert out["status"] == "ok"
    assert out["comps"][0]["id"] == 9

    async with factory() as session:
        cache = await session.get(ListingInspection, lid)
    assert cache.status == "ok"
    assert json.loads(cache.report)["summary"] == "solid"


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_chat_requires_prior_inspection(authed_client, db_factory, monkeypatch):
    @asynccontextmanager
    async def _s():
        async with db_factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.inspections.get_async_session", _s)
    monkeypatch.setattr("dealbot.agents.inspector.get_async_session", _s)

    async with db_factory() as session:
        user = User(email="test@example.com", hashed_password="x")
        user.id = 1
        session.add(user)
        row = _listing()
        session.add(row)
        await session.commit()
        lid = row.id

    resp = authed_client.post(
        f"/listings/{lid}/chat",
        json={"messages": [{"role": "user", "content": "worth it?"}]},
    )
    assert resp.status_code == 409


def test_grounding_includes_playbook_only_with_watchlist():
    from dealbot.api.routes.inspections import _grounding
    from dealbot.schemas import WatchlistContext

    listing = _listing()
    inspection = {"status": "ok", "report": {"summary": "fine"}, "detail": None}

    bare = _grounding(listing, inspection, None, None)
    assert "playbook" not in bare.lower()

    ctx = WatchlistContext(product_query="office chair", max_budget=400.0)
    rich = _grounding(listing, inspection, "What to check\nTilt.", ctx)
    assert "Your playbook" in rich
    assert "budget $400" in rich


# ---------------------------------------------------------------------------
# Plan 3: watches, allowance, auto-inspect
# ---------------------------------------------------------------------------

from dealbot.db.models import InspectionWatch  # noqa: E402


@pytest.fixture()
async def wdb(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _s() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.worker.inspections.get_async_session", _s)
    monkeypatch.setattr("dealbot.api.routes.inspections.get_async_session", _s)
    yield factory
    await engine.dispose()


@pytest.mark.asyncio
async def test_price_drop_notifies_once_and_only_on_drop(wdb, monkeypatch):
    from dealbot.worker import inspections as wi

    factory = wdb
    async with factory() as session:
        u = User(email="drop@example.com", hashed_password="x")
        session.add(u)
        await session.flush()
        dropped = _listing(canonical_url="c1", raw_url="r1", price=200.0)
        risen = _listing(canonical_url="c2", raw_url="r2", price=500.0)
        session.add_all([dropped, risen])
        await session.flush()
        session.add_all([
            InspectionWatch(user_id=u.id, listing_id=dropped.id, price_at_inspection=300.0),
            InspectionWatch(user_id=u.id, listing_id=risen.id, price_at_inspection=400.0),
        ])
        await session.commit()

    sent: list[tuple[str, str]] = []

    async def _fake_send(to, subject, body, html=None):
        sent.append((to, subject))
        return True

    monkeypatch.setattr(wi, "send_email", _fake_send)

    assert await wi.check_price_drops() == 1
    assert len(sent) == 1 and "dropped to $200" in sent[0][1]

    # Second sweep: already notified, nothing new.
    assert await wi.check_price_drops() == 0
    assert len(sent) == 1


@pytest.mark.asyncio
async def test_allowance_caps_free_and_not_pro(wdb):
    from dealbot.api.routes import inspections as ri

    factory = wdb
    async with factory() as session:
        free = User(email="free@example.com", hashed_password="x",
                    inspections_used=ri.FREE_INSPECTIONS_PER_MONTH,
                    inspections_month=ri._month_now())
        pro = User(email="pro@example.com", hashed_password="x", is_pro=True,
                   inspections_used=99, inspections_month=ri._month_now())
        stale = User(email="stale@example.com", hashed_password="x",
                     inspections_used=99, inspections_month="2020-01")
        session.add_all([free, pro, stale])
        await session.commit()
        free_id, pro_id, stale_id = free.id, pro.id, stale.id

    assert await ri._check_allowance(free_id) is False
    assert await ri._check_allowance(pro_id) is True
    # Lazy month reset restores the stale user's allowance.
    assert await ri._check_allowance(stale_id) is True


@pytest.mark.asyncio
async def test_auto_inspect_runs_for_all_owners_top5(wdb, monkeypatch):
    """Redesign spec: top-5 pre-reads for every agent (card teasers depend
    on them), free owners included; already-inspected listings are skipped."""
    from dealbot.db.models import Hunt, ListingAlert, Watchlist
    from dealbot.worker import inspections as wi

    factory = wdb
    async with factory() as session:
        owner = User(email="free2@example.com", hashed_password="x", is_pro=False)
        session.add(owner)
        await session.flush()
        wl = Watchlist(user_id=owner.id, name="Chairs")
        session.add(wl)
        await session.flush()
        hunt = Hunt(watchlist_id=wl.id, status="succeeded")
        session.add(hunt)
        await session.flush()
        listings = [_listing(canonical_url=f"c{i}", raw_url=f"r{i}") for i in range(7)]
        session.add_all(listings)
        await session.flush()
        for i, listing in enumerate(listings):
            session.add(ListingAlert(
                user_id=owner.id, watchlist_id=wl.id, listing_id=listing.id,
                hunt_id=hunt.id, score=0.9 - i * 0.05,
            ))
        # The best-scored listing is already inspected: it must be skipped.
        session.add(ListingInspection(
            listing_id=listings[0].id, status="ok", report="{}",
            detail=ListingDetail().model_dump_json(),
            created_at=datetime.now(timezone.utc),
        ))
        await session.commit()
        hunt_id = hunt.id
        inspected_already = listings[0].id

    called = []

    async def _fake(listing_id, force=False):
        called.append(listing_id)
        return {"status": "ok"}

    monkeypatch.setattr("dealbot.agents.inspector.get_or_create_inspection", _fake)
    assert await wi.auto_inspect_top_matches(hunt_id) == 5
    assert len(called) == 5
    assert inspected_already not in called


def test_price_read_is_deterministic():
    from dealbot.agents.inspector import price_read

    comps = [{"price": p} for p in (80.0, 90.0, 100.0, 110.0, 120.0)]
    assert price_read(100.0, comps) == {"level": "fair", "text": "fair for the market"}
    over = price_read(160.0, comps)
    assert over["level"] == "over" and "over the going rate" in over["text"]
    under = price_read(50.0, comps)
    assert under["level"] == "under"
    assert price_read(100.0, comps[:3]) is None  # no band below 5 comps
