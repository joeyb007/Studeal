from __future__ import annotations

import json
import logging
from typing import Any

from dealbot.agents.tools import TOOL_DEFINITIONS, TOOL_REGISTRY
from dealbot.llm.base import LLMClient
from dealbot.schemas import AlertTier, DealRaw, DealScore

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 6

SYSTEM_PROMPT = """\
You are a deal scoring agent. Your job is to assess the quality of a retail deal \
and return a score from 0 to 100.

Use the available tools to:
1. Verify the discount is genuine (not inflated from a fake "original" price)
2. Check the price history to assess value vs historical pricing

When you have enough information, respond with ONLY a JSON object in this exact shape \
and no other text:
{
  "score": <integer 0-100>,
  "alert_tier": <"none" | "digest" | "push">,
  "category": <normalised slug e.g. "electronics/audio" or "home/kitchen">,
  "tags": [<short tag strings>],
  "real_discount_pct": <float or null>,
  "confidence": "high"
}

Scoring rubric:
- 0-30:  weak — small discount or inflated original price
- 31-60: decent — genuine discount but not exceptional
- 61-80: good — meaningful discount vs historical price
- 81-100: exceptional — significant discount, price near all-time low

Alert tiers:
- "none":   score < 50
- "digest": score 50-79
- "push":   score >= 80"""


def _deal_to_text(deal: DealRaw) -> str:
    lines = [
        f"Title: {deal.title}",
        f"Source: {deal.source}",
        f"Listed price: ${deal.listed_price}",
        f"Sale price: ${deal.sale_price}",
        f"URL: {deal.url}",
    ]
    if deal.asin:
        lines.append(f"ASIN: {deal.asin}")
    if deal.description:
        lines.append(f"Description: {deal.description}")
    return "\n".join(lines)


def _low_confidence_score(deal: DealRaw) -> DealScore:
    """Fallback emitted when the agent hits MAX_ITERATIONS without finishing."""
    return DealScore(
        deal=deal,
        score=30,
        alert_tier=AlertTier.none,
        category="unknown",
        tags=[],
        real_discount_pct=None,
        confidence="low",
    )


class ScorerAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def score(
        self,
        deal: DealRaw,
        similar_context: str | None = None,
    ) -> DealScore:
        system = SYSTEM_PROMPT
        if similar_context:
            system = system + "\n\n" + similar_context

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": _deal_to_text(deal)},
        ]

        for iteration in range(MAX_ITERATIONS):
            response = await self._llm.complete(messages, tools=TOOL_DEFINITIONS)

            # No tool calls — model is done reasoning, parse the final score
            if not response.tool_calls:
                return self._parse_score(response.content, deal)

            # Append the assistant turn to the conversation history
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in response.tool_calls
                ],
            })

            # Execute each requested tool and append results
            for tc in response.tool_calls:
                tool_fn = TOOL_REGISTRY.get(tc.name)
                if tool_fn is None:
                    logger.warning("LLM requested unknown tool: %s", tc.name)
                    tool_result = {"error": f"unknown tool '{tc.name}'"}
                else:
                    result = await tool_fn(**tc.arguments)  # type: ignore[operator]
                    tool_result = result.model_dump()

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": json.dumps(tool_result),
                })

            logger.debug("ScorerAgent iteration %d/%d", iteration + 1, MAX_ITERATIONS)

        # Exceeded MAX_ITERATIONS
        logger.warning("ScorerAgent hit max iterations for deal: %s", deal.title)
        return _low_confidence_score(deal)

    def _parse_score(self, content: str | None, deal: DealRaw) -> DealScore:
        if not content:
            logger.warning("ScorerAgent received empty content, returning low-confidence score")
            return _low_confidence_score(deal)

        try:
            data = json.loads(content)
            return DealScore(deal=deal, **data)
        except Exception:
            # Retry once — strip any markdown fences and try again
            cleaned = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            try:
                data = json.loads(cleaned)
                return DealScore(deal=deal, **data)
            except Exception:
                logger.warning("ScorerAgent failed to parse DealScore, returning low-confidence score")
                return _low_confidence_score(deal)
