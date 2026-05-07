from __future__ import annotations

import json
from typing import Any

import pytest

from dealbot.agents.scorer import ScorerAgent
from dealbot.llm.base import LLMClient, LLMResponse, ToolCall
from dealbot.schemas import AlertTier, DealRaw


# --- Mock LLMClient ---------------------------------------------------------

class MockLLMClient(LLMClient):
    """Returns a pre-scripted sequence of LLMResponse objects."""

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
    "category": "Electronics",
    "tags": ["headphones", "sony", "50-off"],
    "real_discount_pct": 50.0,
    "confidence": "high",
})


# --- Tests -------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scorer_calls_tool_then_scores():
    """Model calls fetch_price_history on turn 1, returns DealScore on turn 2."""
    llm = MockLLMClient([
        LLMResponse(
            content=None,
            tool_calls=[
                ToolCall(
                    id="call_1",
                    name="fetch_price_history",
                    arguments={"asin": "B09XS7JWHH"},
                )
            ],
        ),
        LLMResponse(content=VALID_SCORE_JSON, tool_calls=[]),
    ])

    scorer = ScorerAgent(llm=llm)
    result = await scorer.score(DEAL)

    assert result.score == 82
    assert result.alert_tier == AlertTier.push
    assert result.category == "Electronics"
    assert result.confidence == "high"
    assert llm.call_count == 2


@pytest.mark.asyncio
async def test_scorer_returns_immediately_without_tools():
    """Model skips tool calls and returns a DealScore directly."""
    llm = MockLLMClient([
        LLMResponse(content=VALID_SCORE_JSON, tool_calls=[]),
    ])

    scorer = ScorerAgent(llm=llm)
    result = await scorer.score(DEAL)

    assert result.score == 82
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_scorer_emits_low_confidence_on_max_iterations():
    """Model keeps calling tools past the 6-iteration limit."""
    repeated_tool_call = LLMResponse(
        content=None,
        tool_calls=[
            ToolCall(
                id="call_n",
                name="fetch_price_history",
                arguments={"asin": "B09XS7JWHH"},
            )
        ],
    )

    llm = MockLLMClient([repeated_tool_call])  # returns same response forever
    scorer = ScorerAgent(llm=llm)
    result = await scorer.score(DEAL)

    assert result.confidence == "low"
    assert result.score >= 0
    assert llm.call_count == 6
