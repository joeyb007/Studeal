"""QueryGenerator

Given a WatchlistContext, produces N distinct search phrasings for the hunt.
Multiple queries expand marketplace coverage: 'Herman Miller Aeron chair',
'Aeron desk chair', 'used office chair Herman Miller' all surface different
listing sets even on the same site.

Single LLM call. Fresh context per invocation.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


DEFAULT_QUERY_COUNT = 3


class _QueriesJSON(BaseModel):
    queries: list[str]


QUERY_GENERATOR_SYSTEM = """You produce diverse search query phrasings for a
marketplace deal-hunter. Given a user's product spec, emit 3-4 distinct
query strings that a human would plausibly type into a marketplace search
bar. Diversity is the goal — vary synonyms, word order, and specificity.

Return: {"queries": ["...", "...", "..."]}

Rules:
  - Every query must be a plausible marketplace search string (short, no
    filler words like "please find").
  - No duplicate queries.
  - Do not add price filters or location keywords — that's handled elsewhere.
  - Stay focused on the product; don't drift to related products.
  - The spec may carry a buyer_profile: who this person is and what they value.
    Use it to choose WHICH queries to emit — a profile mentioning back problems
    makes "ergonomic lumbar support chair" a good query. NEVER copy profile
    prose into a query. Queries stay short marketplace search strings; the
    profile never lengthens them or adds narrative."""


class QueryGenerator:
    def __init__(self, llm: LLMClient, count: int = DEFAULT_QUERY_COUNT) -> None:
        self.llm = llm
        self.count = count

    async def generate(self, spec: WatchlistContext) -> list[str]:
        """Emit `count` distinct query phrasings. Falls back to [product_query]
        on LLM/parse failure — always returns at least one query."""
        user = (
            f"Product spec: {spec.model_dump_json(indent=2)}\n\n"
            f"Emit exactly {self.count} distinct query strings as JSON."
        )
        messages = [
            {"role": "system", "content": QUERY_GENERATOR_SYSTEM},
            {"role": "user", "content": user},
        ]

        try:
            response = await self.llm.complete(
                messages, response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("QueryGenerator: LLM call failed: %s", exc)
            return [spec.product_query]

        try:
            data = json.loads(response.content)
            parsed = _QueriesJSON.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("QueryGenerator: parse failed: %s", exc)
            return [spec.product_query]

        # Dedup + strip + cap at self.count.
        seen: set[str] = set()
        result: list[str] = []
        for q in parsed.queries:
            q = q.strip()
            if not q:
                continue
            key = q.lower()
            if key in seen:
                continue
            seen.add(key)
            result.append(q)
            if len(result) >= self.count:
                break

        # Always include original as fallback if LLM emitted nothing usable.
        if not result:
            result.append(spec.product_query)
        return result
