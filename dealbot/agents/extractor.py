from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient
from dealbot.schemas import DealRaw
from dealbot.search.client import FetchedPage

logger = logging.getLogger(__name__)

_MAX_DISCOUNT_PCT = 95.0


def _price_grounded(price: float, text: str) -> bool:
    """Return True if the price string appears literally in the source text."""
    candidates = [
        f"{price:.2f}",
        f"{price:.0f}",
        f"${price:.2f}",
        f"${price:.0f}",
    ]
    return any(c in text for c in candidates)


def _prices_valid(listed: float, sale: float, text: str) -> bool:
    if sale <= 0 or listed <= 0:
        return False
    if sale > listed:
        return False
    discount_pct = (listed - sale) / listed * 100
    if discount_pct > _MAX_DISCOUNT_PCT:
        return False
    if not _price_grounded(sale, text):
        return False
    if not _price_grounded(listed, text):
        return False
    return True


_SYSTEM_PROMPT = """\
You are a product deal extractor. Given the text content of a webpage, extract deal information if present.

Return ONLY a JSON object with these exact fields:
{
  "title": "full product name",
  "listed_price": 99.99,
  "sale_price": 69.99,
  "url": "https://...",
  "source": "domain name e.g. amazon.com",
  "description": "one sentence description or null"
}

Rules:
- listed_price must be the original/RRP price, sale_price the discounted price
- If only one price is shown with no clear discount, set both listed_price and sale_price to that value
- If no product with pricing information is found, return null
- Return null for review articles, blog posts, or pages with no purchasable product
- Numbers only for prices, no currency symbols"""


class ExtractorAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def extract(self, page: FetchedPage) -> DealRaw | None:
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"URL: {page.url}\n\n{page.text}"},
        ]

        try:
            response = await self._llm.complete(messages)
            content = (response.content or "").strip()

            if content.lower() == "null" or not content:
                return None

            data = json.loads(content)
            if data is None:
                return None

            listed = float(data["listed_price"])
            sale = float(data["sale_price"])

            if not _prices_valid(listed, sale, page.text):
                logger.debug("ExtractorAgent: prices failed grounding check for %s", page.url)
                return None

            return DealRaw(
                source=data["source"],
                title=data["title"],
                url=page.url,
                listed_price=listed,
                sale_price=sale,
                description=data.get("description"),
            )
        except Exception:
            logger.warning("ExtractorAgent: failed to extract from %s", page.url)
            return None
