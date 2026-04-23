from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a search query generator for a deal-hunting agent targeting students and budget-conscious shoppers.

Given a product or category keyword, generate exactly 4 search query variants that will surface \
PRODUCT LISTING PAGES on retail sites — pages where a specific item can actually be purchased at a \
discounted price.

Rules:
- Queries must target retailer product pages (amazon.com, bestbuy.com, walmart.com, target.com, costco.com, newegg.com, bhphotovideo.com)
- Use site: operators when helpful, e.g. "Sony WH-1000XM4 site:amazon.com"
- Include specific product names or model numbers where you know them
- Include transactional terms like "sale", "deal", "price drop", "discount", "clearance"
- Do NOT generate queries that would surface review articles, roundups, or editorial content
- Do NOT use queries like "best X for students" — these return review sites, not product pages

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
