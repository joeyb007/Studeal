from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Deal
from dealbot.graph.graph import build_scorer_graph
from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import DealRaw


class MockLLMClient(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        **kwargs: Any,
    ) -> LLMResponse:
        response = self._responses[min(self.call_count, len(self._responses) - 1)]
        self.call_count += 1
        return response


DEAL = DealRaw(
    source="bestbuy.ca",
    title="Sony WH-1000XM5 Wireless Headphones",
    url="https://www.bestbuy.ca/en-ca/product/12345",
    listed_price=349.99,
    sale_price=174.99,
)

LEGITIMATE_JSON = json.dumps({
    "legitimate": True,
    "validation_confidence": 0.92,
    "validation_reason": "Plausible price from a known retailer.",
    "category": "Audio",
    "condition": "new",
    "student_eligible": False,
    "real_discount_pct": 50.0,
    "tags": ["headphones", "sony"],
})

REJECTION_JSON = json.dumps({
    "legitimate": False,
    "validation_confidence": 0.95,
    "validation_reason": "Price implausibly low — likely a scam.",
    "category": "Audio",
    "condition": "unknown",
    "student_eligible": False,
    "real_discount_pct": None,
    "tags": [],
})


@pytest.fixture()
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def patch_session(db_session_factory, monkeypatch):
    factory = db_session_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.graph.nodes.get_async_session", _test_session)
    return factory


@pytest.fixture()
def patch_embed(monkeypatch):
    """Stub out embed_text so tests don't hit the OpenAI embeddings API."""
    async def _fake_embed(text: str):
        return None

    monkeypatch.setattr("dealbot.graph.nodes.embed_text", _fake_embed)


@pytest.mark.asyncio
async def test_pipeline_persists_legitimate_deal(patch_session, patch_embed):
    llm = MockLLMClient([LLMResponse(content=LEGITIMATE_JSON, tool_calls=[])])
    app = build_scorer_graph(llm)

    result = await app.ainvoke({"deal": DEAL})

    assert result["outcome"] == "persisted_legitimate"
    assert "deal_id" in result

    async with patch_session() as session:
        rows = (await session.execute(select(Deal))).scalars().all()

    assert len(rows) == 1
    row = rows[0]
    assert row.title == "Sony WH-1000XM5 Wireless Headphones"
    assert row.legitimate is True
    assert row.deal_score is not None
    assert 0 <= row.deal_score <= 100


@pytest.mark.asyncio
async def test_pipeline_persists_rejected_deal(patch_session, patch_embed):
    """Rejected deals are persisted with legitimate=False, not dropped."""
    llm = MockLLMClient([LLMResponse(content=REJECTION_JSON, tool_calls=[])])
    app = build_scorer_graph(llm)

    result = await app.ainvoke({"deal": DEAL})

    assert result["outcome"] == "persisted_rejected"

    async with patch_session() as session:
        rows = (await session.execute(select(Deal))).scalars().all()

    assert len(rows) == 1
    assert rows[0].legitimate is False
    assert rows[0].deal_score == 0


@pytest.mark.asyncio
async def test_pipeline_deduplicates_by_url(patch_session, patch_embed):
    """Running the pipeline twice for the same URL writes only one row."""
    llm = MockLLMClient([LLMResponse(content=LEGITIMATE_JSON, tool_calls=[])])
    app = build_scorer_graph(llm)

    await app.ainvoke({"deal": DEAL})
    await app.ainvoke({"deal": DEAL})

    async with patch_session() as session:
        rows = (await session.execute(select(Deal))).scalars().all()

    assert len(rows) == 1


@pytest.mark.asyncio
async def test_pipeline_llm_failure_persists_as_rejected(patch_session, patch_embed):
    """LLM exceptions are caught by ScorerAgent and produce a fallback rejection.
    The deal is still persisted (legitimate=False) so it's not silently lost."""
    class FailingLLM(LLMClient):
        async def complete(self, messages, **kwargs):
            raise RuntimeError("LLM unavailable")

    app = build_scorer_graph(FailingLLM())
    result = await app.ainvoke({"deal": DEAL})

    assert result["outcome"] == "persisted_rejected"

    async with patch_session() as session:
        rows = (await session.execute(select(Deal))).scalars().all()

    assert len(rows) == 1
    assert rows[0].legitimate is False
