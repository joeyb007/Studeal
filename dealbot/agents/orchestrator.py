from __future__ import annotations

import json
import logging
from typing import Any

from dealbot.agents.extractor import ExtractorAgent
from dealbot.agents.query_gen import QueryGenAgent
from dealbot.llm.base import LLMClient
from dealbot.schemas import DealRaw
from dealbot.scrapers.browser_agent import fetch_page, search_shopping
from dealbot.search.client import FetchedPage

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 12
_MAX_PAGE_CHARS = 6_000  # chars fed back as tool result — keeps context window manageable

_SYSTEM_PROMPT = """\
You are a deal-hunting research agent for students and budget-conscious shoppers.

Goal: find real discounted product listings for the given keyword. Each deal needs:
- A specific product name and model number
- A real current price visible on the page (never estimate or invent prices)
- A direct purchase URL on a retailer site (amazon, bestbuy, walmart, target, newegg, etc.)

You have these tools:

generate_queries
  Generate 3-4 focused search query variants for a keyword. Call this first.

search_shopping
  Search Google Shopping using a stealth browser. Returns rendered page text.
  Use transactional queries — include model numbers, "sale", "deal", site: operators.

fetch_page
  Fetch a specific product page URL using a stealth browser. Returns rendered page text.
  Use this to get pricing details when search_shopping returns a category/results page.

extract_products
  Extract structured deal data from page text you have already retrieved.
  Pass the page text and its source URL. Returns a summary of what was found.
  Call this after every search_shopping or fetch_page that looks promising.

finish
  Signal that research is complete. Call when you have found 5+ deals or exhausted options.

Strategy:
1. Call generate_queries to get focused variants
2. Call search_shopping with your most specific query
3. Call extract_products on the result
4. If results are thin, try fetch_page on individual product URLs from the page, then extract_products again
5. Try up to 3 different queries
6. Call finish when done

Never invent prices. Only extract what is literally on the page."""

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_queries",
            "description": "Generate focused search query variants for a keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "The product or category to search for"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_shopping",
            "description": "Search Google Shopping using a stealth browser. Returns rendered page text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a specific URL using a stealth browser. Returns rendered page text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute URL to fetch"},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "extract_products",
            "description": (
                "Extract structured deal data from page text. "
                "Call this after search_shopping or fetch_page."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Page text content to extract from"},
                    "source_url": {"type": "string", "description": "URL the text came from"},
                },
                "required": ["text", "source_url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": "Signal that research is complete.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]


class OrchestratorAgent:
    """
    ReAct orchestrator. Holds the tool loop and dispatches to specialised agents.

    Tool call flow:
      generate_queries → QueryGenAgent
      search_shopping  → browser_agent.search_shopping (Browserbase CDP)
      fetch_page       → browser_agent.fetch_page (Browserbase CDP)
      extract_products → ExtractorAgent (specialised LLM extraction)
      finish           → terminates loop, returns collected candidates
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._extractor = ExtractorAgent(llm=llm)

    async def run(self, keyword: str) -> list[DealRaw]:
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": f"Find deals for: {keyword}"},
        ]
        candidates: list[DealRaw] = []

        for iteration in range(_MAX_ITERATIONS):
            try:
                response = await self._llm.complete(messages, tools=_TOOL_DEFINITIONS)
            except Exception as exc:
                logger.warning("OrchestratorAgent: LLM call failed on iteration %d: %s", iteration, exc)
                break

            if not response.tool_calls:
                # LLM responded with text but no tool call — treat as done
                logger.info("OrchestratorAgent: no tool call on iteration %d, stopping", iteration)
                break

            # Append assistant turn (with tool calls) to history
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in response.tool_calls
                ],
            })

            # Execute each tool call and append results
            done = False
            for tc in response.tool_calls:
                result, found = await self._dispatch(tc, candidates)
                candidates.extend(found)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })

                if tc.name == "finish":
                    done = True

            if done:
                break

        logger.info("OrchestratorAgent: %d candidates for %r", len(candidates), keyword)
        return candidates

    async def _dispatch(
        self, tc: Any, existing: list[DealRaw]
    ) -> tuple[str, list[DealRaw]]:
        """Execute a tool call. Returns (tool_result_text, newly_found_deals)."""
        try:
            if tc.name == "generate_queries":
                return await self._tool_generate_queries(tc.arguments)

            elif tc.name == "search_shopping":
                return await self._tool_search_shopping(tc.arguments)

            elif tc.name == "fetch_page":
                return await self._tool_fetch_page(tc.arguments)

            elif tc.name == "extract_products":
                return await self._tool_extract_products(tc.arguments)

            elif tc.name == "finish":
                return "Research complete.", []

            else:
                logger.warning("OrchestratorAgent: unknown tool %r", tc.name)
                return f"Unknown tool: {tc.name}", []

        except Exception as exc:
            logger.warning("OrchestratorAgent: tool %r raised %s", tc.name, exc)
            return f"Tool error: {exc}", []

    async def _tool_generate_queries(self, args: dict) -> tuple[str, list[DealRaw]]:
        keyword = args.get("keyword", "")
        queries = await QueryGenAgent(llm=self._llm).generate(keyword)
        return json.dumps(queries), []

    async def _tool_search_shopping(self, args: dict) -> tuple[str, list[DealRaw]]:
        query = args.get("query", "")
        text = await search_shopping(query)
        if not text:
            return "No content retrieved — page may have been blocked.", []
        return text[:_MAX_PAGE_CHARS], []

    async def _tool_fetch_page(self, args: dict) -> tuple[str, list[DealRaw]]:
        url = args.get("url", "")
        if not url.startswith("http"):
            return "Invalid URL.", []
        text = await fetch_page(url)
        if not text:
            return "No content retrieved.", []
        return text[:_MAX_PAGE_CHARS], []

    async def _tool_extract_products(self, args: dict) -> tuple[str, list[DealRaw]]:
        text = args.get("text", "")
        source_url = args.get("source_url", "unknown")

        if not text.strip():
            return "No text provided.", []

        # ExtractorAgent expects a FetchedPage — wrap the text
        page = FetchedPage(url=source_url, text=text[:_MAX_PAGE_CHARS], links=[])
        deal = await self._extractor.extract(page)

        if deal is None:
            return "No deal found in this page.", []

        summary = (
            f"Extracted: {deal.title} — ${deal.sale_price:.2f} "
            f"(was ${deal.listed_price:.2f}) from {deal.source}"
        )
        logger.info("OrchestratorAgent: %s", summary)
        return summary, [deal]
