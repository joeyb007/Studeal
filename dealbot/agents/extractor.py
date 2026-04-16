from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from dealbot.llm.base import LLMClient
from dealbot.schemas import DealRaw
from dealbot.search.client import FetchedPage

logger = logging.getLogger(__name__)

_MAX_DISCOUNT_PCT = 95.0
_MAX_HOPS = 3
_FETCH_TIMEOUT = 10
_MAX_TEXT_CHARS = 4000

_TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "fetch",
            "description": (
                "Fetch the content of a URL. Use this when the current page does not "
                "contain product pricing and a link looks like it leads to a product page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch"},
                },
                "required": ["url"],
            },
        },
    }
]

_SYSTEM_PROMPT = """\
You are a product deal extractor. Given the text content of a webpage and a list of links \
found on that page, extract deal information if present.

You may call the fetch tool at most once per response to navigate to a more specific product \
page if the current page does not contain pricing information (e.g. you are on a search results \
or category page). Only follow links that look like individual product pages.

When you have found a product with pricing, return ONLY a JSON object with these exact fields:
{
  "title": "full product name",
  "listed_price": 99.99,
  "sale_price": 69.99,
  "url": "https://...",
  "source": "domain name e.g. amazon.com",
  "description": "one sentence description or null",
  "student_eligible": false
}

Rules:
- listed_price must be the original/RRP price, sale_price the discounted price
- If only one price is shown with no clear discount, set both listed_price and sale_price to that value
- If no product with pricing information is found, return null
- Return null for review articles, blog posts, or pages with no purchasable product
- Numbers only for prices, no currency symbols
- Set student_eligible to true only if the page explicitly mentions student pricing, \
a student discount programme (UNiDAYS, Student Beans, .edu), or requires student verification"""


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


async def _fetch_page(url: str) -> FetchedPage | None:
    """Fetch a URL and return a FetchedPage, or None on failure."""
    try:
        async with httpx.AsyncClient(timeout=_FETCH_TIMEOUT, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()
            html = resp.text

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
            tag.decompose()

        text = soup.get_text(separator=" ", strip=True)[:_MAX_TEXT_CHARS]

        base_url = str(resp.url)
        links: list[str] = []
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if href.startswith("http"):
                links.append(href)
            elif href.startswith("/"):
                parsed = urlparse(base_url)
                links.append(f"{parsed.scheme}://{parsed.netloc}{href}")
        links = list(dict.fromkeys(links))[:20]

        return FetchedPage(url=base_url, text=text, links=links)
    except Exception:
        logger.debug("ExtractorAgent: failed to fetch %s", url)
        return None


def _page_to_user_message(page: FetchedPage) -> str:
    links_section = "\n".join(page.links) if page.links else "none"
    return f"URL: {page.url}\n\nPage content:\n{page.text}\n\nLinks on this page:\n{links_section}"


def _parse_deal(content: str, page: FetchedPage) -> DealRaw | None:
    """Parse LLM JSON output into a DealRaw, with grounding validation."""
    content = content.strip()
    if content.lower() == "null" or not content:
        return None
    try:
        data = json.loads(content)
    except json.JSONDecodeError:
        cleaned = content.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        try:
            data = json.loads(cleaned)
        except Exception:
            return None

    if data is None:
        return None

    try:
        listed = float(data["listed_price"])
        sale = float(data["sale_price"])
    except (KeyError, TypeError, ValueError):
        return None

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
        student_eligible=bool(data.get("student_eligible", False)),
    )


class ExtractorAgent:
    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def extract(self, page: FetchedPage) -> DealRaw | None:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _page_to_user_message(page)},
        ]

        current_page = page

        for hop in range(_MAX_HOPS):
            try:
                response = await self._llm.complete(messages, tools=_TOOL_DEFINITIONS)
            except Exception:
                logger.warning("ExtractorAgent: LLM call failed on hop %d for %s", hop, page.url)
                return None

            # No tool call — LLM is done, parse the final answer
            if not response.tool_calls:
                return _parse_deal(response.content or "", current_page)

            # LLM called fetch — navigate to the next page
            tc = response.tool_calls[0]
            if tc.name != "fetch":
                logger.warning("ExtractorAgent: unexpected tool call '%s'", tc.name)
                return None

            next_url = tc.arguments.get("url", "")
            logger.debug("ExtractorAgent: hop %d → %s", hop + 1, next_url)

            next_page = await _fetch_page(next_url)
            if next_page is None:
                # Navigation failed — ask LLM to try something else or give up
                tool_result = "Error: could not fetch that URL."
            else:
                current_page = next_page
                tool_result = _page_to_user_message(next_page)

            # Append assistant turn + tool result to conversation history
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                ],
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": tool_result,
            })

        logger.debug("ExtractorAgent: hit max hops for %s, returning None", page.url)
        return None
