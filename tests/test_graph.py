from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from dealbot.graph.graph import build_graph
from dealbot.graph.nodes import DB_PATH
from dealbot.llm.base import LLMClient, LLMResponse, ToolCall
from dealbot.schemas import AlertTier, DealRaw


# --- Mock LLMClient (same pattern as test_scorer.py) ------------------------

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


@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    """Point DB_PATH at a temp file so tests don't pollute deals.db."""
    test_db = tmp_path / "test_deals.db"
    monkeypatch.setattr("dealbot.graph.nodes.DB_PATH", test_db)
    yield test_db


# --- Tests -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_graph_scores_and_persists(clean_db):
    """Full pipeline: DealRaw in → DealScore out → row written to SQLite."""
    llm = MockLLMClient([
        LLMResponse(content=VALID_SCORE_JSON, tool_calls=[]),
    ])

    app = build_graph(llm)
    final_state = await app.ainvoke({"deal": DEAL})

    # Score result is in state
    assert "score_result" in final_state
    result = final_state["score_result"]
    assert result.score == 82
    assert result.alert_tier == AlertTier.push
    assert result.confidence == "high"
    assert "error" not in final_state

    # Row was written to SQLite
    conn = sqlite3.connect(clean_db)
    rows = conn.execute("SELECT title, score, alert_tier FROM deals").fetchall()
    conn.close()
    assert len(rows) == 1
    assert rows[0] == ("Sony WH-1000XM5 Headphones", 82, "push")


@pytest.mark.asyncio
async def test_graph_skips_persist_on_score_error(clean_db):
    """If scoring raises, error is set in state and persist is skipped."""
    class BrokenLLMClient(LLMClient):
        async def complete(self, messages, tools=None):
            raise RuntimeError("LLM unavailable")

    app = build_graph(BrokenLLMClient())
    final_state = await app.ainvoke({"deal": DEAL})

    assert "error" in final_state
    assert "score_result" not in final_state

    # Nothing written to DB
    conn = sqlite3.connect(clean_db)
    rows = conn.execute("SELECT * FROM deals").fetchall()
    conn.close()
    assert len(rows) == 0
