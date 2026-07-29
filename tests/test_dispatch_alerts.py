"""Tests for dispatch_alerts — new listings → scored alerts → notifications.

`_dispatch` is tested directly with sqlite, a fake reranker, a recorded
send_email, and a fake publisher. The contract: candidates are this hunt's
was_new_for_watchlist listings, budget is a hard pre-filter, rerank gates by
score (identity fallback keeps candidates), rows are capped per hunt, one
summary email goes out, and alert.created events are published.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Hunt, HuntListing, Listing, ListingAlert, User, Watchlist
from dealbot.events.publisher import RedisEventPublisher
from dealbot.rerank.service import RerankResult


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str):
        self.published.append((channel, message))

    async def aclose(self):
        pass


class FakeRerank:
    """Returns preset scores positionally; records the documents it saw."""

    def __init__(self, scores: list[float]):
        self.scores = scores
        self.seen_documents: list[str] | None = None

    async def rerank(self, query, documents, top_n=20):
        self.seen_documents = list(documents)
        return [
            RerankResult(index=i, relevance_score=s)
            for i, s in enumerate(self.scores[: len(documents)])
        ]


@pytest.fixture()
async def rig(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    @asynccontextmanager
    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as s:
            yield s

    import dealbot.worker.alerts as alerts_mod

    monkeypatch.setattr(alerts_mod, "get_async_session", _session)

    sent_emails: list[tuple[str, str, str]] = []

    async def fake_send_email(to, subject, body):
        sent_emails.append((to, subject, body))
        return True

    monkeypatch.setattr(alerts_mod, "send_email", fake_send_email)

    fake_redis = FakeRedis()
    publisher = RedisEventPublisher(client=fake_redis)
    yield factory, alerts_mod, fake_redis, publisher, sent_emails
    await engine.dispose()


async def _seed(factory, *, prices: list[float], new_flags: list[bool] | None = None,
                context: str = '{"product_query": "aeron"}') -> int:
    """Seed user/watchlist/hunt + one listing per price. Returns hunt_id."""
    new_flags = new_flags or [True] * len(prices)
    async with factory() as s:
        user = User(email="a@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="Aeron watch", context=context)
        s.add(wl)
        await s.flush()
        hunt = Hunt(watchlist_id=wl.id)
        s.add(hunt)
        await s.flush()
        for i, (price, is_new) in enumerate(zip(prices, new_flags)):
            listing = Listing(
                canonical_url=f"https://k.ca/{i}", raw_url=f"https://k.ca/{i}",
                marketplace="kijiji", title=f"Aeron {i}", price=price, currency="CAD",
            )
            s.add(listing)
            await s.flush()
            s.add(HuntListing(hunt_id=hunt.id, listing_id=listing.id,
                              was_new_for_watchlist=is_new))
        hunt_id = hunt.id
        await s.commit()
        return hunt_id


@pytest.mark.asyncio
async def test_alert_rows_created_above_threshold(rig):
    factory, alerts_mod, fake_redis, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110, 120, 130],
                          new_flags=[True, True, True, False])
    rerank = FakeRerank([0.9, 0.4, 0.1])  # only the 3 new candidates are scored
    result = await alerts_mod._dispatch(hunt_id, rerank=rerank, publisher=publisher)

    assert result["alerts"] == 2  # 0.9 and 0.4 clear the 0.30 threshold
    assert len(rerank.seen_documents) == 3  # was_new=False listing never scored
    async with factory() as s:
        rows = (await s.execute(select(ListingAlert))).scalars().all()
        assert sorted(r.score for r in rows) == [0.4, 0.9]
        assert all("feed" in r.channels for r in rows)


@pytest.mark.asyncio
async def test_budget_hard_filter(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(
        factory, prices=[250, 400],
        context='{"product_query": "aeron", "max_budget": 300}',
    )
    rerank = FakeRerank([0.9, 0.9])
    await alerts_mod._dispatch(hunt_id, rerank=rerank, publisher=publisher)
    assert len(rerank.seen_documents) == 1  # 400 excluded before rerank


@pytest.mark.asyncio
async def test_identity_fallback_keeps_candidates(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    rerank = FakeRerank([0.0, 0.0])  # Cohere down → identity fallback scores
    result = await alerts_mod._dispatch(hunt_id, rerank=rerank, publisher=publisher)
    assert result["alerts"] == 2


@pytest.mark.asyncio
async def test_cap_per_hunt(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100 + i for i in range(7)])
    rerank = FakeRerank([0.9] * 7)
    result = await alerts_mod._dispatch(hunt_id, rerank=rerank, publisher=publisher)
    assert result["alerts"] == 5  # ALERT_MAX_PER_HUNT default


@pytest.mark.asyncio
async def test_email_sent_once_and_channels_updated(rig):
    factory, alerts_mod, _, publisher, sent_emails = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    result = await alerts_mod._dispatch(
        hunt_id, rerank=FakeRerank([0.9, 0.8]), publisher=publisher,
    )
    assert result["emailed"] is True
    assert len(sent_emails) == 1
    to, subject, body = sent_emails[0]
    assert to == "a@t.com"
    assert "2 new matches" in subject
    async with factory() as s:
        rows = (await s.execute(select(ListingAlert))).scalars().all()
        assert all("email" in r.channels for r in rows)


@pytest.mark.asyncio
async def test_alert_created_events_published(rig):
    factory, alerts_mod, fake_redis, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    await alerts_mod._dispatch(hunt_id, rerank=FakeRerank([0.9, 0.8]), publisher=publisher)
    events = [json.loads(m) for _, m in fake_redis.published]
    created = [e for e in events if e["type"] == "alert.created"]
    assert len(created) == 2
    assert {e["title"] for e in created} == {"Aeron 0", "Aeron 1"}
    assert all(e["url"].startswith("https://k.ca/") for e in created)


@pytest.mark.asyncio
async def test_no_candidates_is_noop(rig):
    factory, alerts_mod, fake_redis, publisher, sent_emails = rig
    hunt_id = await _seed(factory, prices=[100], new_flags=[False])
    result = await alerts_mod._dispatch(
        hunt_id, rerank=FakeRerank([0.9]), publisher=publisher,
    )
    assert result == {"alerts": 0, "emailed": False, "pushed": 0}
    assert sent_emails == [] and fake_redis.published == []
