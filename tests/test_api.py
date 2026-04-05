from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from collections.abc import AsyncGenerator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.api.main import app
from dealbot.api.routes.deals import router
from dealbot.db.models import Base, Deal


# --- In-memory DB fixture ----------------------------------------------------

@pytest.fixture()
async def db_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def client(db_factory, monkeypatch):
    """TestClient backed by an in-memory SQLite DB."""
    factory = db_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.api.routes.deals.get_async_session", _test_session)
    return TestClient(app)


# --- Seed helper -------------------------------------------------------------

async def _seed_deal(factory, **overrides) -> Deal:
    defaults = dict(
        title="Sony WH-1000XM5",
        source="slickdeals",
        url="https://example.com/deal/1",
        listed_price=349.99,
        sale_price=174.99,
        asin="B09XS7JWHH",
        score=82,
        alert_tier="push",
        category="electronics/audio",
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


# --- Tests -------------------------------------------------------------------

def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


@pytest.mark.asyncio
async def test_list_deals_empty(client):
    resp = client.get("/deals")
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_list_deals_returns_seeded_row(client, db_factory):
    await _seed_deal(db_factory)
    resp = client.get("/deals")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Sony WH-1000XM5"
    assert data[0]["score"] == 82
    assert data[0]["alert_tier"] == "push"


@pytest.mark.asyncio
async def test_list_deals_filter_by_tier(client, db_factory):
    await _seed_deal(db_factory, alert_tier="push", title="Deal A")
    await _seed_deal(db_factory, alert_tier="digest", title="Deal B", url="https://example.com/2")
    resp = client.get("/deals?tier=push")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["title"] == "Deal A"


@pytest.mark.asyncio
async def test_get_deal_by_id(client, db_factory):
    deal = await _seed_deal(db_factory)
    resp = client.get(f"/deals/{deal.id}")
    assert resp.status_code == 200
    assert resp.json()["id"] == deal.id
    assert resp.json()["title"] == "Sony WH-1000XM5"


def test_get_deal_not_found(client):
    resp = client.get("/deals/99999")
    assert resp.status_code == 404
    assert resp.json()["detail"] == "Deal not found"
