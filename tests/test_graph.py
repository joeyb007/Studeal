from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from collections.abc import AsyncGenerator

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from dealbot.db.models import Base, Deal
from dealbot.graph.graph import build_graph
from dealbot.llm.base import LLMClient, LLMResponse, ToolCall
from dealbot.schemas import AlertTier, DealRaw


# --- Mock LLMClient ----------------------------------------------------------

class MockLLMClient(LLMClient):
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self.call_count = 0

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        response = self._responses[min(self.call_count, len(self._responses) - 1)]
        self.call_count += 1
        return response


# --- Fixtures ----------------------------------------------------------------

DEAL = DealRaw(
    source="slickdeals",
    title="Sony WH-1000XM5 Headphones",
    url="https://example.com/deal/123",
    listed_price=349.99,
    sale_price=174.99,
    asin="B09XS7JWHH",
)

VALID_SCORE_JSON = json.dumps({
    "score": 82,
    "alert_tier": "push",
    "category": "electronics/audio",
    "tags": ["headphones", "sony", "50-off"],
    "real_discount_pct": 50.0,
    "confidence": "high",
})


@pytest.fixture()
async def db_session_factory():
    """In-memory SQLite engine with schema created. Yields the sessionmaker."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture()
def patch_session(db_session_factory, monkeypatch):
    """Replace get_async_session with one backed by the in-memory test DB."""
    factory = db_session_factory

    @asynccontextmanager
    async def _test_session() -> AsyncGenerator[AsyncSession, None]:
        async with factory() as session:
            yield session

    monkeypatch.setattr("dealbot.graph.nodes.get_async_session", _test_session)
    return factory


# --- Tests -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_scores_and_persists(patch_session):
    """Full pipeline: DealRaw in → DealScore out → row written to DB."""
    llm = MockLLMClient([
        LLMResponse(content=VALID_SCORE_JSON, tool_calls=[]),
    ])

    app = build_graph(llm)
    final_state = await app.ainvoke({"deal": DEAL})

    assert "score_result" in final_state
    result = final_state["score_result"]
    assert result.score == 82
    assert result.alert_tier == AlertTier.push
    assert result.confidence == "high"
    assert "error" not in final_state

    # Row was written to DB
    async with patch_session() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(Deal)
        )).scalars().all()

    assert len(rows) == 1
    assert rows[0].title == "Sony WH-1000XM5 Headphones"
    assert rows[0].score == 82
    assert rows[0].alert_tier == "push"


@pytest.mark.asyncio
async def test_graph_deduplicates_by_url(patch_session):
    """Running the pipeline twice for the same URL writes only one row."""
    llm = MockLLMClient([
        LLMResponse(content=VALID_SCORE_JSON, tool_calls=[]),
    ])

    app = build_graph(llm)
    await app.ainvoke({"deal": DEAL})
    await app.ainvoke({"deal": DEAL})  # same URL — should be silently skipped

    async with patch_session() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(Deal)
        )).scalars().all()

    assert len(rows) == 1


@pytest.mark.asyncio
async def test_graph_skips_persist_on_score_error(patch_session):
    """If scoring raises, error is set in state and persist is skipped."""
    class BrokenLLMClient(LLMClient):
        async def complete(self, messages, tools=None):
            raise RuntimeError("LLM unavailable")

    app = build_graph(BrokenLLMClient())
    final_state = await app.ainvoke({"deal": DEAL})

    assert "error" in final_state
    assert "score_result" not in final_state

    # Nothing written to DB
    async with patch_session() as session:
        rows = (await session.execute(
            __import__("sqlalchemy").select(Deal)
        )).scalars().all()

    assert len(rows) == 0
