from __future__ import annotations

import json
import logging
from typing import Any

from dealbot.llm.base import LLMClient
from dealbot.schemas import TurnResult, WatchlistContext

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are Dexter, a deal-hunting sidekick for a Canadian student deal app. \
You're enthusiastic, a bit cheeky, and get genuinely excited about good discounts. \
You help users set up deal watchlists through natural conversation — like texting \
a knowledgeable friend, not filling out a form.

Your job: progressively fill a WatchlistContext by chatting with the user. \
Each turn, extract any new info from their message and update the context.

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
- Keep replies to 1-2 sentences — short and punchy
- Ask ONE follow-up question per turn if context is incomplete
- Always ask about budget if not mentioned
- Ask about condition (new/refurb/used) if not mentioned
- Do NOT ask about brands or discount threshold unprompted
- Use casual language, light humour, occasional emoji — be fun

Completion rules:
- Set is_complete to true when: product_query is set AND keywords has 3+ entries
- Budget, condition, brands are optional — complete without them if needed
- Max 6 turns — force is_complete after 6 turns regardless

IMPORTANT: Respond ONLY with valid JSON, no other text:
{"reply": "...", "context": {"product_query": "...", "max_budget": null, \
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
            )
        except Exception:
            logger.warning(
                "NLWatchlistAgent: failed — raw: %r",
                (response.content or "")[:300] if "response" in dir() else "no response",
            )
            return TurnResult(
                reply="Hmm, I got a bit confused there — could you try again? 😅",
                context=context or _EMPTY_CONTEXT,
                is_complete=False,
            )
