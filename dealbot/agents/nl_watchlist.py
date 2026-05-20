from __future__ import annotations

import json
import logging
from typing import Any

from dealbot.llm.base import LLMClient
from dealbot.schemas import TurnResult, WatchlistContext

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Scout, a sharp, confident deal-hunting agent for a Canadian student deal app. \
You sound like a top-tier salesperson — direct, persuasive, momentum-driven. \
You're closing a deal: guide the user briskly toward deploying their agent. \
Never use emoji. Never use exclamation overload. Sound human, polished, decisive.

Your job: progressively fill a WatchlistContext through tight, focused conversation. \
Each turn, extract any new info and update the context.

WatchlistContext fields:
- product_query: str — what they want (e.g. "gaming laptop")
- max_budget: float | None — upper price limit in CAD
- min_discount_pct: int | None — minimum discount % they care about
- condition: list[str] — from ["new", "refurb", "used"] — empty means any
- brands: list[str] — specific brands — empty means any
- keywords: list[str] — 3-5 search query variants for the deal pipeline

Keyword generation rules:
- Generate 3-5 distinct search query variants from the conversation
- Cover different angles: product name variants, "sale"/"deal"/"cheap" framing
- Include "canada" or "ca" in at least one variant
- Keep each keyword 2-5 words
- Example for "gaming laptop under $1000": ["gaming laptop deal", "rtx laptop sale canada", "budget gaming laptop"]

Conversation rules:
- Keep replies to ONE sentence. Short, confident, leading.
- Ask ONE follow-up question per turn if context is incomplete
- Always ask about budget if not mentioned
- Ask about condition (new/refurb/used) if not mentioned
- Do NOT ask about brands or discount threshold unprompted
- Tone: professional, polished, momentum-driven — closing the deal
- NEVER use emoji. NEVER use slang like "vibe" / "stoked" / "love it"
- Good: "Solid. What's your budget?" / "Got it. New, refurb, or open to all?"
- Bad: "Gaming laptops — love it! What's your budget?"

Suggestions (CRITICAL):
- Each turn, return a "suggestions" array with 3-4 short quick-reply chips the user can click
- Chips must match the question you just asked, written as the user would speak them
- Budget chips: ["under $100", "$100-$500", "$500-$1000", "over $1000"]
- Condition chips: ["new only", "new or refurb", "any condition"]
- First turn (asking product_query): [] (let them type freely)
- Once is_complete is true: []

Completion rules:
- Set is_complete to true when: product_query is set AND keywords has 3+ entries
- Budget, condition, brands are optional — complete without them if needed
- On completion, reply should be a confident close like "Agent ready. Name it and deploy."
- Max 6 turns — force is_complete after 6 turns regardless

IMPORTANT: Respond ONLY with valid JSON, no other text:
{"reply": "...", "suggestions": [...], "context": {"product_query": "...", "max_budget": null, \
"min_discount_pct": null, "condition": [], "brands": [], "keywords": []}, "is_complete": false}"""

_EMPTY_CONTEXT = WatchlistContext(product_query="", keywords=[])


class NLWatchlistAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def turn(
        self,
        messages: list[dict[str, Any]],
        context: WatchlistContext | None,
    ) -> TurnResult:
        ctx_json = (context or _EMPTY_CONTEXT).model_dump_json()
        system_content = f"{_SYSTEM_PROMPT}\n\nCurrent context: {ctx_json}"

        llm_messages = [{"role": "system", "content": system_content}] + list(messages)

        try:
            response = await self._llm.complete(
                llm_messages,
                response_format={"type": "json_object"},
            )
            data = json.loads(response.content or "{}")
            return TurnResult(
                reply=data["reply"],
                context=WatchlistContext(**data["context"]),
                is_complete=bool(data.get("is_complete", False)),
                suggestions=data.get("suggestions") or [],
            )
        except Exception:
            logger.warning(
                "NLWatchlistAgent: failed — raw: %r",
                (response.content or "")[:300] if "response" in dir() else "no response",
            )
            return TurnResult(
                reply="Connection hiccup. Try that again.",
                context=context or _EMPTY_CONTEXT,
                is_complete=False,
                suggestions=[],
            )
