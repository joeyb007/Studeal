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
