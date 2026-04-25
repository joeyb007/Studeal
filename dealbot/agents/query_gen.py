from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a search query generator for a deal-hunting agent targeting students and budget-conscious shoppers.

Given a product or category keyword, generate exactly 4 search query variants optimised for \
Google Shopping (tbm=shop). These queries are submitted directly to the Google Shopping tab — \
NOT regular Google Search — so site: operators do NOT work and must never be used.

Rules:
- Include the full product name and model number (e.g. "Sony WH-1000XM4")
- Add retailer names as plain words when helpful (e.g. "amazon", "bestbuy", "walmart")
- Include transactional terms like "sale", "deal", "price drop", "discount", "clearance"
- Do NOT use site: operators — they return zero results on Google Shopping
- Do NOT generate vague category queries — be specific to the product

Return ONLY a JSON array of 4 strings. No other text, no markdown."""


class QueryGenAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def generate(self, keyword: str) -> list[str]:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": keyword},
        ]

        try:
            response = await self._llm.complete(messages)
            content = (response.content or "").strip()
            queries = json.loads(content)
            if isinstance(queries, list) and all(isinstance(q, str) for q in queries):
                return [q for q in queries if q.strip()]
        except Exception:
            logger.warning("QueryGenAgent: failed to generate queries for %r", keyword)

        return [keyword]
