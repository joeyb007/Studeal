from __future__ import annotations

import json
from typing import Any

import pytest

from dealbot.agents.scorer import ScorerAgent
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

VALID_VALIDATION_JSON = json.dumps({
    "legitimate": True,
    "validation_confidence": 0.92,
    "validation_reason": "Plausible price for Sony WH-1000XM5 at 50% off from a known retailer.",
    "category": "Audio",
    "condition": "new",
    "student_eligible": False,
    "real_discount_pct": 50.0,
    "tags": ["headphones", "sony", "50-off"],
})

REJECTION_JSON = json.dumps({
    "legitimate": False,
    "validation_confidence": 0.97,
    "validation_reason": "Price is implausibly low — likely a scam listing.",
    "category": "Audio",
    "condition": "unknown",
    "student_eligible": False,
    "real_discount_pct": None,
    "tags": [],
})


@pytest.mark.asyncio
async def test_validate_legitimate_deal():
    llm = MockLLMClient([LLMResponse(content=VALID_VALIDATION_JSON, tool_calls=[])])
    result = await ScorerAgent(llm=llm).validate(DEAL)

    assert result.legitimate is True
    assert result.validation_confidence == pytest.approx(0.92)
    assert result.category.value == "Audio"
    assert result.condition.value == "new"
    assert result.real_discount_pct == pytest.approx(50.0)
    assert llm.call_count == 1


@pytest.mark.asyncio
async def test_validate_rejected_deal():
    llm = MockLLMClient([LLMResponse(content=REJECTION_JSON, tool_calls=[])])
    result = await ScorerAgent(llm=llm).validate(DEAL)

    assert result.legitimate is False
    assert result.validation_confidence == pytest.approx(0.97)
    assert "implausibly" in result.validation_reason


@pytest.mark.asyncio
async def test_validate_empty_response_falls_back_to_rejection():
    llm = MockLLMClient([LLMResponse(content=None, tool_calls=[])])
    result = await ScorerAgent(llm=llm).validate(DEAL)

    assert result.legitimate is False
    assert result.validation_confidence == 0.0
    assert "empty" in result.validation_reason


@pytest.mark.asyncio
async def test_validate_invalid_json_falls_back_to_rejection():
    llm = MockLLMClient([LLMResponse(content="not json at all", tool_calls=[])])
    result = await ScorerAgent(llm=llm).validate(DEAL)

    assert result.legitimate is False
    assert result.validation_confidence == 0.0


@pytest.mark.asyncio
async def test_validate_llm_exception_falls_back_to_rejection():
    class FailingLLM(LLMClient):
        async def complete(self, messages, **kwargs):
            raise RuntimeError("network error")

    result = await ScorerAgent(llm=FailingLLM()).validate(DEAL)

    assert result.legitimate is False
    assert "LLM error" in result.validation_reason


@pytest.mark.asyncio
async def test_score_method_raises():
    """score() was removed — callers that haven't migrated should fail loudly."""
    llm = MockLLMClient([LLMResponse(content=VALID_VALIDATION_JSON, tool_calls=[])])
    with pytest.raises(RuntimeError, match="removed"):
        await ScorerAgent(llm=llm).score(DEAL)
