from __future__ import annotations

import json
import json as json_lib
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import WatchlistContext, WatchlistContextPatch


class MockLLM(LLMClient):
    def __init__(self, content: str) -> None:
        self._content = content
        self.call_count = 0

    async def complete(self, messages: list[dict[str, Any]], tools=None) -> LLMResponse:
        self.call_count += 1
        return LLMResponse(content=self._content, tool_calls=[])


def test_watchlist_context_defaults():
    ctx = WatchlistContext(product_query="gaming laptop", keywords=["gaming laptop deal"])
    assert ctx.max_budget is None
    assert ctx.min_discount_pct is None
    assert ctx.condition == []
    assert ctx.brands == []


def test_watchlist_context_full():
    ctx = WatchlistContext(
        product_query="gaming laptop",
        max_budget=1000.0,
        min_discount_pct=20,
        condition=["new", "refurb"],
        brands=["Asus", "Dell"],
        keywords=["gaming laptop deal", "rtx laptop sale", "budget gaming laptop"],
    )
    assert ctx.max_budget == 1000.0
    assert len(ctx.keywords) == 3


def test_watchlist_context_patch_all_optional():
    patch = WatchlistContextPatch()
    assert patch.max_budget is None
    assert patch.min_discount_pct is None
    assert patch.condition is None
    assert patch.brands is None


@pytest.mark.asyncio
async def test_agent_extracts_product_query():
    from dealbot.agents.nl_watchlist import NLWatchlistAgent

    response_json = json.dumps({
        "reply": "Gaming laptops — great choice! What's your budget?",
        "context": {
            "product_query": "gaming laptop",
            "max_budget": None,
            "min_discount_pct": None,
            "condition": [],
            "brands": [],
            "keywords": ["gaming laptop deal", "rtx laptop sale canada"],
        },
        "is_complete": False,
    })
    llm = MockLLM(response_json)
    agent = NLWatchlistAgent(llm)

    result = await agent.turn(
        messages=[{"role": "user", "content": "I want gaming laptop deals"}],
        context=None,
    )

    assert result.context.product_query == "gaming laptop"
    assert result.is_complete is False
    assert result.reply != ""


@pytest.mark.asyncio
async def test_agent_completes_when_enough_context():
    from dealbot.agents.nl_watchlist import NLWatchlistAgent

    response_json = json.dumps({
        "reply": "Perfect, I'm on it! 🔥",
        "context": {
            "product_query": "gaming laptop",
            "max_budget": 1000.0,
            "min_discount_pct": None,
            "condition": ["new"],
            "brands": [],
            "keywords": ["gaming laptop deal", "rtx laptop sale canada", "budget gaming laptop"],
        },
        "is_complete": True,
    })
    llm = MockLLM(response_json)
    agent = NLWatchlistAgent(llm)

    result = await agent.turn(
        messages=[
            {"role": "user", "content": "gaming laptop"},
            {"role": "assistant", "content": "What's your budget?"},
            {"role": "user", "content": "under $1000, new only"},
        ],
        context=None,
    )

    assert result.is_complete is True
    assert len(result.context.keywords) >= 3


@pytest.mark.asyncio
async def test_agent_falls_back_on_bad_json():
    from dealbot.agents.nl_watchlist import NLWatchlistAgent

    llm = MockLLM("this is not json at all")
    agent = NLWatchlistAgent(llm)

    result = await agent.turn(
        messages=[{"role": "user", "content": "I want headphones"}],
        context=None,
    )

    assert result.reply != ""
    assert result.is_complete is False
