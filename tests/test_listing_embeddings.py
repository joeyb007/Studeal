"""Listing embeddings: producer/consumer split.

The hunt path persists with embed=False (NULL vectors, no serial embedding
tail) and the consumer (`embed_pending_for_hunt`) fills them afterwards; the
link-in/fetch path keeps the default synchronous embed. Embeddings remain an
ENHANCEMENT: no path may lose a listing because the embedding service is down.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.workers.extractor import Offer
from dealbot.db.models import Base, Hunt, HuntListing, Listing, User, Watchlist
from dealbot.llm.embeddings import EMBED_DIM


@pytest.fixture()
async def factory(monkeypatch):
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    f = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)

    import dealbot.persistence.listings as mod

    @asynccontextmanager
    async def _session() -> AsyncGenerator[AsyncSession, None]:
        async with f() as s:
            yield s

    monkeypatch.setattr(mod, "get_async_session", _session)
    yield f, mod
    await engine.dispose()


def _offer(url: str, **kw) -> Offer:
    base = dict(title="Aeron chair", price=500.0, currency="CAD",
                url=url, marketplace="kijiji", condition="used",
                location="Toronto, ON")
    base.update(kw)
    return Offer(**base)


def test_embed_text_composition():
    from dealbot.persistence.listings import listing_embed_text
    text = listing_embed_text(_offer("https://k.ca/1"))
    assert "Aeron chair" in text
    assert "used" in text
    assert "kijiji" in text
    assert "Toronto" in text


def test_row_embed_text_matches_offer_composition():
    """Producer and consumer must embed into the identical space."""
    from dealbot.persistence.listings import listing_embed_text, listing_row_embed_text
    offer = _offer("https://k.ca/1")
    row = Listing(
        title=offer.title, condition=offer.condition,
        marketplace=offer.marketplace, location=offer.location,
    )
    assert listing_row_embed_text(row) == listing_embed_text(offer)


# ---- producer side: persist_offers -----------------------------------------


@pytest.mark.asyncio
async def test_default_embed_path_batches_once(factory, monkeypatch):
    f, mod = factory
    calls: list[list] = []

    async def fake_embed_listings(items):
        calls.append(items)
        return [[0.5] * EMBED_DIM for _ in items]

    monkeypatch.setattr(mod, "embed_listings", fake_embed_listings)
    await mod.persist_offers([_offer("https://k.ca/1"), _offer("https://k.ca/2")])

    assert len(calls) == 1, f"expected one batched call, got {len(calls)}"
    assert len(calls[0]) == 2
    async with f() as s:
        rows = (await s.execute(select(Listing))).scalars().all()
        assert rows and all(r.embedding is not None for r in rows)


@pytest.mark.asyncio
async def test_embed_false_skips_the_service_entirely(factory, monkeypatch):
    f, mod = factory

    async def exploding_embed_listings(items):
        raise AssertionError("embed=False must never call the service")

    monkeypatch.setattr(mod, "embed_listings", exploding_embed_listings)
    result = await mod.persist_offers([_offer("https://k.ca/1")], embed=False)

    assert result.written == 1
    async with f() as s:
        row = (await s.execute(select(Listing))).scalar_one()
        assert row.embedding is None


@pytest.mark.asyncio
async def test_embedding_failure_still_persists(factory, monkeypatch):
    f, mod = factory

    async def failing_embed_listings(items):
        raise RuntimeError("embedding service down")

    monkeypatch.setattr(mod, "embed_listings", failing_embed_listings)
    result = await mod.persist_offers([_offer("https://k.ca/3")])

    assert result.written == 1, "persistence must survive embedding failure"
    async with f() as s:
        row = (await s.execute(select(Listing))).scalar_one()
        assert row.embedding is None


@pytest.mark.asyncio
async def test_reupsert_without_vector_keeps_existing_embedding(factory, monkeypatch):
    """A hunt re-sighting (embed=False) must not null the vector the consumer
    already wrote — the on-conflict clause skips the embedding column."""
    f, mod = factory

    async def fake_embed_listings(items):
        return [[0.5] * EMBED_DIM for _ in items]

    url = "https://k.ca/keep"
    monkeypatch.setattr(mod, "embed_listings", fake_embed_listings)
    await mod.persist_offers([_offer(url)])                    # embeds
    await mod.persist_offers([_offer(url, price=450.0)], embed=False)

    async with f() as s:
        row = (await s.execute(select(Listing))).scalar_one()
        assert row.embedding is not None, "re-sight must not erase the vector"
        assert row.price == 450.0, "the rest of the refresh still applies"


# ---- consumer side: embed_pending_for_hunt / embed_orphan_listings ----------


async def _seeded_hunt(f) -> tuple[int, list[int]]:
    """A hunt with two NULL-embedding listings linked, one already embedded."""
    async with f() as s:
        user = User(email="e@t.com", hashed_password="x")
        s.add(user)
        await s.flush()
        wl = Watchlist(user_id=user.id, name="aeron", context="{}")
        s.add(wl)
        await s.flush()
        hunt = Hunt(watchlist_id=wl.id)
        s.add(hunt)
        await s.flush()
        rows = [
            Listing(canonical_url=f"https://k.ca/c{i}", raw_url=f"https://k.ca/c{i}",
                    marketplace="kijiji", title=f"Aeron {i}", price=100.0 + i,
                    currency="CAD", condition="used")
            for i in range(3)
        ]
        rows[2].embedding = [0.1] * EMBED_DIM
        s.add_all(rows)
        await s.flush()
        for r in rows:
            s.add(HuntListing(hunt_id=hunt.id, listing_id=r.id))
        await s.commit()
        return hunt.id, [r.id for r in rows]


@pytest.mark.asyncio
async def test_consumer_embeds_only_pending_rows(factory, monkeypatch):
    f, mod = factory
    hunt_id, ids = await _seeded_hunt(f)
    embedded_texts: list[str] = []

    async def fake_embed_listings(items):
        embedded_texts.extend(t for t, _img in items)
        return [[0.9] * EMBED_DIM for _ in items]

    monkeypatch.setattr(mod, "embed_listings", fake_embed_listings)
    count = await mod.embed_pending_for_hunt(hunt_id)

    assert count == 2, "only the two NULL rows get embedded"
    async with f() as s:
        rows = (await s.execute(select(Listing))).scalars().all()
        assert all(r.embedding is not None for r in rows)


@pytest.mark.asyncio
async def test_consumer_failure_leaves_nulls_for_healer(factory, monkeypatch):
    f, mod = factory
    hunt_id, ids = await _seeded_hunt(f)

    async def failing_embed_listings(items):
        raise RuntimeError("bedrock down")

    monkeypatch.setattr(mod, "embed_listings", failing_embed_listings)
    count = await mod.embed_pending_for_hunt(hunt_id)

    assert count == 0
    async with f() as s:
        nulls = (await s.execute(
            select(Listing).where(Listing.embedding.is_(None))
        )).scalars().all()
        assert len(nulls) == 2, "failure must not lose or corrupt rows"


@pytest.mark.asyncio
async def test_healer_sweeps_orphans(factory, monkeypatch):
    f, mod = factory
    await _seeded_hunt(f)

    async def fake_embed_listings(items):
        return [[0.9] * EMBED_DIM for _ in items]

    monkeypatch.setattr(mod, "embed_listings", fake_embed_listings)
    count = await mod.embed_orphan_listings()

    assert count == 2
    async with f() as s:
        nulls = (await s.execute(
            select(Listing).where(Listing.embedding.is_(None))
        )).scalars().all()
        assert nulls == []
