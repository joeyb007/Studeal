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


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str):
        self.published.append((channel, message))

    async def aclose(self):
        pass


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
    ranker = FakeRanker([0.9, 0.4, 0.1])  # only the 3 new candidates are scored
    result = await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)

    assert result["alerts"] == 2  # 0.9 and 0.4 clear the 0.30 threshold
    assert len(ranker.seen_titles) == 3  # was_new=False listing never scored
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
    ranker = FakeRanker([0.9, 0.9])
    await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)
    assert len(ranker.seen_titles) == 1  # 400 excluded before rerank


@pytest.mark.asyncio
async def test_identity_fallback_keeps_candidates(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    ranker = FakeRanker([0.0, 0.0])  # Cohere down → identity fallback scores
    result = await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)
    assert result["alerts"] == 2


@pytest.mark.asyncio
async def test_cap_per_hunt(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100 + i for i in range(7)])
    ranker = FakeRanker([0.9] * 7)
    result = await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)
    assert result["alerts"] == 5  # ALERT_MAX_PER_HUNT default


@pytest.mark.asyncio
async def test_email_sent_once_and_channels_updated(rig):
    factory, alerts_mod, _, publisher, sent_emails = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    result = await alerts_mod._dispatch(
        hunt_id, ranker=FakeRanker([0.9, 0.8]), publisher=publisher,
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
    await alerts_mod._dispatch(hunt_id, ranker=FakeRanker([0.9, 0.8]), publisher=publisher)
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
        hunt_id, ranker=FakeRanker([0.9]), publisher=publisher,
    )
    assert result == {"alerts": 0, "emailed": False, "pushed": 0}
    assert sent_emails == [] and fake_redis.published == []


# ---------------------------------------------------------------------------
# Listwise ranking with reasons (Workstream C)
# ---------------------------------------------------------------------------

class FakeRanker:
    """Positional scores + a reason each. Records what it was asked to rank."""

    def __init__(self, scores: list[float], reason: str = "Fits your setup."):
        self.scores = scores
        self.reason = reason
        self.seen_titles: list[str] | None = None
        self.seen_spec = None

    async def __call__(self, spec, candidates, *, llm=None, top_n=20):
        from dealbot.recsys.ranker import RankedListing

        self.seen_spec = spec
        self.seen_titles = [c.title for c in candidates]
        ranked = [
            RankedListing(listing=c, score=s, reason=self.reason)
            for c, s in zip(candidates, self.scores)
        ]
        ranked.sort(key=lambda r: r.score, reverse=True)
        return ranked[:top_n]


@pytest.mark.asyncio
async def test_alerts_persist_a_reason(rig):
    """The reason is the product: an alert that says why beats one that ranks."""
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    ranker = FakeRanker([0.9, 0.8], reason="Over-ear ANC, well under budget.")

    result = await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)

    assert result["alerts"] == 2
    async with factory() as s:
        rows = (await s.execute(select(ListingAlert))).scalars().all()
    assert all(r.reason == "Over-ear ANC, well under budget." for r in rows)


@pytest.mark.asyncio
async def test_candidates_stay_provenance_scoped(rig):
    """Alerts rank this hunt's new listings — not the whole pool. Dense
    retrieval belongs on the read path, not here."""
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(factory, prices=[100, 110, 120],
                          new_flags=[True, True, False])
    ranker = FakeRanker([0.9, 0.8])
    await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)
    assert ranker.seen_titles == ["Aeron 0", "Aeron 1"]


@pytest.mark.asyncio
async def test_ranker_receives_the_full_context_including_profile(rig):
    factory, alerts_mod, _, publisher, _ = rig
    hunt_id = await _seed(
        factory, prices=[100],
        context=json.dumps({
            "product_query": "aeron",
            "buyer_profile": "Remote worker with back problems.",
        }),
    )
    ranker = FakeRanker([0.9])
    await alerts_mod._dispatch(hunt_id, ranker=ranker, publisher=publisher)
    assert ranker.seen_spec.buyer_profile == "Remote worker with back problems."


@pytest.mark.asyncio
async def test_internal_user_gets_alert_rows_but_no_delivery(rig):
    """House hunts must feed the pool and the alert feed — but never Resend:
    bounces to @studeal.internal damage sender reputation for real alerts."""
    factory, alerts_mod, _, publisher, sent_emails = rig
    hunt_id = await _seed(factory, prices=[100, 110])
    async with factory() as s:
        user = (await s.execute(select(User))).scalars().one()
        user.email = "house@studeal.internal"
        await s.commit()

    result = await alerts_mod._dispatch(
        hunt_id, ranker=FakeRanker([0.9, 0.8]), publisher=publisher,
    )

    assert result["alerts"] == 2, "alert rows still created"
    assert result["emailed"] is False
    assert sent_emails == [], "no delivery attempt to an internal address"
