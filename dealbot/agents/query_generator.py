"""QueryGenerator

Given a WatchlistContext, produces the hunt's search queries. The user's own
product_query always runs verbatim; the LLM adds complementary phrasings that
cover inventory the core query misses (brand/model angles, seller-title
vocabulary). Synonym rephrases are explicitly not wanted — marketplace search
engines already fuzzy-match those, so they return the same listings and just
burn browser lanes.

Single LLM call. Fresh context per invocation.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)


DEFAULT_QUERY_COUNT = 2


class _QueriesJSON(BaseModel):
    queries: list[str]


QUERY_GENERATOR_SYSTEM = """You produce complementary search queries for a
marketplace deal-hunter. The user's own query already runs verbatim — your
job is to add coverage it misses, not to rephrase it.

Return: {"queries": ["..."]}

Rules:
  - Each query must be meaningfully different from the user's own phrasing:
    a specific brand or model the spec implies, or a distinct angle sellers
    actually put in listing titles. Synonym shuffles and word-order swaps
    are worthless — marketplace search engines already match those.
  - When the spec lists must-tier attributes, weave them into your query
    using words sellers actually put in titles: handedness, set composition,
    included accessories ("right handed complete golf club set with bag").
    Must-tier attributes are the single best source of a complementary query.
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
        """Return `count` queries: the user's product_query first, then
        LLM-generated complements. Falls back to [product_query] on LLM/parse
        failure — always returns at least one query."""
        core = spec.product_query.strip()
        result: list[str] = [core]
        seen: set[str] = {core.lower()}
        if self.count <= 1:
            return result

        user = (
            f"Product spec: {spec.model_dump_json(indent=2)}\n\n"
            f"The query {core!r} already runs as-is. Emit exactly "
            f"{self.count - 1} complementary query strings as JSON."
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
            return result

        try:
            data = json.loads(response.content)
            parsed = _QueriesJSON.model_validate(data)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning("QueryGenerator: parse failed: %s", exc)
            return result

        # Dedup + strip + cap at self.count total (core included).
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

        return result
