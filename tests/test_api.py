from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dealbot.db.models import Deal


async def _seed_deal(factory, **overrides) -> Deal:
    from sqlalchemy.ext.asyncio import AsyncSession
    defaults = dict(
        title="Sony WH-1000XM5",
        source="bestbuy.ca",
        url="https://example.com/deal/1",
        listed_price=349.99,
        sale_price=174.99,
        asin="B09XS7JWHH",
        deal_score=72,
        category="Electronics",
        tags=json.dumps(["headphones", "sony"]),
        confidence="high",
        real_discount_pct=50.0,
        scraped_at=datetime.now(timezone.utc),
    )
    defaults.update(overrides)
    async with factory() as session:
        deal = Deal(**defaults)
        session.add(deal)
        await session.commit()
        await session.refresh(deal)
    return deal


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_deals_empty(authed_client):
    resp = authed_client.get("/deals")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_deals_returns_seeded_row(authed_client, db_factory):
    await _seed_deal(db_factory)
    resp = authed_client.get("/deals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Sony WH-1000XM5"
    assert data[0]["deal_score"] == 72


@pytest.mark.asyncio
async def test_list_deals_ordered_by_deal_score(authed_client, db_factory):
    await _seed_deal(db_factory, deal_score=40, title="Weak Deal")
    await _seed_deal(db_factory, deal_score=85, title="Strong Deal", url="https://example.com/2")
    resp = authed_client.get("/deals")
    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["title"] == "Strong Deal"
    assert data[1]["title"] == "Weak Deal"


@pytest.mark.asyncio
async def test_get_deal_by_id(authed_client, db_factory):
    deal = await _seed_deal(db_factory)
    resp = authed_client.get(f"/deals/{deal.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == deal.id
    assert resp.json()["title"] == "Sony WH-1000XM5"


def test_get_deal_not_found(authed_client):
    resp = authed_client.get("/deals/99999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Deal not found"


@pytest.mark.asyncio
async def test_watchlist_exposes_authoritative_sweep_state(authed_client, db_factory):
    """Picks/playbook/all-matches stay sealed until a sweep REPORTS BACK.
    last_hunt_at cannot carry that meaning — creation stamps it to claim the
    cadence slot — and "listings is empty" cannot either, because the read
    path lazily ranks the shared pool. An agent mid-sweep was handed five
    picks it had not found (2026-08-18), so the API answers directly."""
    from dealbot.db.models import Hunt, User, Watchlist

    async with db_factory() as s:
        s.add(User(id=1, email="t@t.com", hashed_password="x"))
        await s.flush()
        queued = Watchlist(user_id=1, name="Queued", context='{"product_query": "a"}')
        hunting = Watchlist(user_id=1, name="Hunting", context='{"product_query": "b"}')
        done = Watchlist(user_id=1, name="Done", context='{"product_query": "c"}')
        s.add_all([queued, hunting, done])
        await s.flush()
        s.add_all([
            Hunt(watchlist_id=hunting.id, status="running"),
            Hunt(watchlist_id=done.id, status="succeeded"),
        ])
        await s.commit()

    rows = {w["name"]: w for w in authed_client.get("/watchlists").json()}

    # Dispatched, no Hunt row yet -> "starting up", nothing unsealed.
    assert rows["Queued"]["hunt_queued"] is True
    assert rows["Queued"]["first_hunt_done"] is False

    # Browsing -> live, but still sealed: it hasn't reported back.
    assert rows["Hunting"]["running_hunt_id"] is not None
    assert rows["Hunting"]["hunt_queued"] is False
    assert rows["Hunting"]["first_hunt_done"] is False

    # Reported back -> unsealed.
    assert rows["Done"]["first_hunt_done"] is True
