from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from dealbot.db.models import Deal, Watchlist, WatchlistKeyword, User
from dealbot.api.auth import hash_password
from dealbot.worker.matching import run_matching


async def _seed_user(factory, email="joe@example.com") -> User:
    async with factory() as session:
        user = User(email=email, hashed_password=hash_password("secret"))
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


async def _seed_watchlist(factory, user_id: int, keywords: list[str], min_score: int = 50) -> Watchlist:
    async with factory() as session:
        wl = Watchlist(user_id=user_id, name="Test list", min_score=min_score)
        session.add(wl)
        await session.flush()
        for kw in keywords:
            session.add(WatchlistKeyword(watchlist_id=wl.id, keyword=kw))
        await session.commit()
        await session.refresh(wl)
    return wl


async def _seed_deal(factory, title="Sony Headphones", score=82, category="electronics") -> Deal:
    async with factory() as session:
        deal = Deal(
            title=title,
            source="slickdeals",
            url=f"https://example.com/{title}",
            listed_price=349.99,
            sale_price=174.99,
            score=score,
            alert_tier="push",
            category=category,
            tags=json.dumps([]),
            confidence="high",
            scraped_at=datetime.now(timezone.utc),
        )
        session.add(deal)
        await session.commit()
        await session.refresh(deal)
    return deal


@pytest.mark.asyncio
async def test_matching_creates_alert(db_factory):
    user = await _seed_user(db_factory)
    await _seed_watchlist(db_factory, user.id, keywords=["sony"])
    deal = await _seed_deal(db_factory, title="Sony WH-1000XM5", score=82)

    async with db_factory() as session:
        count = await run_matching(deal, session)

    assert count == 1


@pytest.mark.asyncio
async def test_matching_respects_min_score(db_factory):
    user = await _seed_user(db_factory)
    await _seed_watchlist(db_factory, user.id, keywords=["sony"], min_score=90)
    deal = await _seed_deal(db_factory, title="Sony WH-1000XM5", score=70)

    async with db_factory() as session:
        count = await run_matching(deal, session)

    assert count == 0


@pytest.mark.asyncio
async def test_matching_no_keyword_match(db_factory):
    user = await _seed_user(db_factory)
    await _seed_watchlist(db_factory, user.id, keywords=["xbox"])
    deal = await _seed_deal(db_factory, title="Sony WH-1000XM5", score=82)

    async with db_factory() as session:
        count = await run_matching(deal, session)

    assert count == 0


@pytest.mark.asyncio
async def test_matching_deduplicates_alerts(db_factory):
    user = await _seed_user(db_factory)
    await _seed_watchlist(db_factory, user.id, keywords=["sony"])
    deal = await _seed_deal(db_factory, title="Sony WH-1000XM5", score=82)

    async with db_factory() as session:
        await run_matching(deal, session)
    async with db_factory() as session:
        count = await run_matching(deal, session)

    # Second run should produce 0 new alerts (duplicate skipped)
    assert count == 0


def test_alerts_endpoint_requires_auth(client):
    resp = client.get("/alerts")
    assert resp.status_code == 401
