from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are a keyword extraction assistant for a deal-hunting app.

Given a natural language description of what deals a user wants to track, \
extract 2-5 short search keyword phrases that best represent their intent.

Rules:
- Phrases should be 1-4 words each
- Focus on product types, brands, and key attributes
- Do not include price constraints or words like "deals" or "discount"
- Respond with ONLY a JSON array of strings, no other text

Example:
Input:  "I want deals on Sony or Bose noise cancelling headphones under $200"
Output: ["sony headphones", "bose headphones", "noise cancelling headphones"]"""


async def extract_keywords(description: str, llm: LLMClient) -> list[str]:
    """Use the LLM to extract keyword phrases from a natural language watchlist description.

    Falls back to a single-item list containing the raw description if parsing fails,
    so the watchlist is always created with at least one keyword.
    """
    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": description},
    ]

    try:
        response = await llm.complete(messages)
        content = (response.content or "").strip()
        keywords: list[str] = json.loads(content)
        if isinstance(keywords, list) and all(isinstance(k, str) for k in keywords):
            return [kw.lower().strip() for kw in keywords if kw.strip()]
    except Exception:
        logger.warning("extract_keywords: failed to parse LLM response, using raw description")

    # Fallback: treat the whole description as one keyword
    return [description.lower().strip()]
