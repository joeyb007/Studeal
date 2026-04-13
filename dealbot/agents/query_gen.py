from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a search query generator for a deal-hunting agent targeting students and budget-conscious shoppers.

Given a product or category keyword, generate exactly 4 search query variants designed to surface \
deals, discounts, coupons, and student pricing across the web.

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
