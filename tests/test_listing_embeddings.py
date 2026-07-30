"""Listing embeddings populated at persist time.

Embeddings power semantic search over the pool. They are an ENHANCEMENT:
a hunt must never lose its listings because the embedding service is down.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.agents.workers.extractor import Offer
from dealbot.db.models import Base, Listing


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


@pytest.mark.asyncio
async def test_embeddings_requested_once_per_call(factory, monkeypatch):
    f, mod = factory
    calls: list[list[str]] = []

    async def fake_embed_texts(texts):
        calls.append(texts)
        return [[0.5] * 1536 for _ in texts]

    monkeypatch.setattr(mod, "embed_texts", fake_embed_texts)
    await mod.persist_offers([_offer("https://k.ca/1"), _offer("https://k.ca/2")])

    assert len(calls) == 1, f"expected one batched call, got {len(calls)}"
    assert len(calls[0]) == 2
    async with f() as s:
        rows = (await s.execute(select(Listing))).scalars().all()
        assert rows and all(r.embedding is not None for r in rows)


@pytest.mark.asyncio
async def test_embedding_failure_still_persists(factory, monkeypatch):
    f, mod = factory

    async def failing_embed_texts(texts):
        raise RuntimeError("embedding service down")

    monkeypatch.setattr(mod, "embed_texts", failing_embed_texts)
    result = await mod.persist_offers([_offer("https://k.ca/3")])

    assert result.written == 1, "persistence must survive embedding failure"
    async with f() as s:
        row = (await s.execute(select(Listing))).scalar_one()
        assert row.embedding is None


@pytest.mark.asyncio
async def test_empty_vector_writes_null(factory, monkeypatch):
    f, mod = factory

    async def blank_embed_texts(texts):
        return [[] for _ in texts]

    monkeypatch.setattr(mod, "embed_texts", blank_embed_texts)
    await mod.persist_offers([_offer("https://k.ca/4")])
    async with f() as s:
        row = (await s.execute(select(Listing))).scalar_one()
        assert row.embedding is None
