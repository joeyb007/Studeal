from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote_plus

from dealbot.agents.query_gen import QueryGenAgent
from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import Condition, DealRaw
from dealbot.scrapers.browser_agent import (
    BrowserSession,
    ShoppingResult,
    fetch_page,
    find_url,
    search_shopping,
)
from dealbot.search.brave import BraveSearchClient

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 12
_MAX_SUMMARY_DEALS = 6
# Cap organic results per query — limits resolution sessions and keeps runs fast.
_MAX_ORGANIC_PER_QUERY = 10

_SYSTEM_PROMPT = """\
You are a deal-hunting research agent for students and budget-conscious shoppers in Canada.

Your goal is to find real discounted product listings for the given keyword.

Strategy:
1. Call generate_queries to get 2 focused search variants
2. Call search_shopping for each query (Google Shopping Canada)
3. Call finish when you have enough candidates

URL resolution for organic Google Shopping listings is handled automatically after \
you finish — you do not need to do anything extra.

Only extract prices that are explicitly shown — never estimate or invent prices."""

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_queries",
            "description": "Generate 2 focused search query variants for a keyword.",
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string"},
                },
                "required": ["keyword"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_shopping",
            "description": (
                "Search Google Shopping Canada. Returns deal listings with titles, "
                "prices, merchants, and discount percentages."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_web",
            "description": (
                "Search the web via Brave. Returns URLs and descriptions. "
                "Use when Shopping and deal sites don't have enough coverage. "
                "Follow up with fetch_page on promising URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "Fetch the full text content of a URL. Use on promising links "
                "returned by search_web to extract deal details."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
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

_SHOPPING_EXTRACT_PROMPT = """\
Extract every product listing from the Google Shopping data below.

The data has two INDEPENDENT sections — do NOT merge entries across them.

FEATURED DEALS — each line is a self-contained product with a direct retailer URL:
  Format: "URL: https://retailer.com/product/... | $sale_price [$listed_price] Merchant [extras]"
  Rules:
  - Each featured deal is its own product entry — one URL = one product
  - Set url to the exact URL given (do not modify)
  - Infer title from the URL path slug (e.g. "sony-wh-1000xm4" → "Sony WH-1000XM4")
  - Extract merchant from the text after the price (e.g. "Walmart", "Best Buy")
  - Do NOT set listing_index for featured deals

ORGANIC LISTINGS — numbered lines with title/price/merchant/condition:
  Format: "N. [DISCOUNT%] TITLE Current Price: $sale_price[. Was $listed_price][. Merchant][. Condition]"
  Rules:
  - Set listing_index to the number N shown at the start of the line
  - No url field — omit it entirely
  - Condition: Pre-owned → used, Refurbished → refurb, explicit New → new, else unknown

For each product extract:
- title: product name and model
- price: sale price as float
- listed_price: original price as float — same as price if no discount shown
- merchant: retailer name
- condition: "new" | "used" | "refurb" | "unknown"
- url: exact URL for featured deals only — omit for organic
- listing_index: integer N for organic listings — omit for featured

Return ONLY valid JSON:
{"products": [{"title": "...", "price": 0.0, "listed_price": 0.0, "merchant": "...", \
"condition": "unknown", "url": "...", "listing_index": null}]}

If no products are found, return: {"products": []}"""


class OrchestratorAgent:
    """
    ReAct orchestrator: single BrowserSession for searching, parallel sessions
    for organic URL resolution after the search loop completes.

    Tools:
    - generate_queries: LLM generates search query variants
    - search_shopping: aria snapshot + LLM extraction of Google Shopping Canada
    - search_web: Brave web search for broader coverage
    - fetch_page: fetches a specific URL for detail extraction
    - finish: signals completion
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._candidates: list[DealRaw] = []
        self._browser_session: BrowserSession | None = None

    async def run(self, keyword: str) -> list[DealRaw]:
        self._candidates = []

        # Phase 1: LLM orchestrator — search and collect candidates.
        # Single shared BrowserSession for all search_shopping calls.
        async with BrowserSession() as session:
            self._browser_session = session

            messages: list[dict[str, Any]] = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": f"Find deals for: {keyword}"},
            ]

            for iteration in range(_MAX_ITERATIONS):
                try:
                    response = await self._llm.complete(messages, tools=_TOOL_DEFINITIONS)
                except Exception as exc:
                    logger.warning("OrchestratorAgent: LLM call failed on iteration %d: %s", iteration, exc)
                    break

                if not response.tool_calls:
                    logger.info("OrchestratorAgent: no tool call on iteration %d, stopping", iteration)
                    break

                messages.append(_assistant_message(response))

                done = False
                for tc in response.tool_calls:
                    result = await self._dispatch(tc, keyword)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
                    if tc.name == "finish":
                        done = True

                if done:
                    break

        self._browser_session = None

        # Phase 2: resolve organic URLs in parallel — each find_url call opens
        # its own fresh Browserbase session so Google sees independent traffic.
        await self._resolve_all_organic()

        logger.info("OrchestratorAgent: %d candidates for %r", len(self._candidates), keyword)
        return self._candidates

    async def _resolve_all_organic(self) -> None:
        """
        Post-loop: resolve every candidate still holding a google.com placeholder URL.
        Deduplicates by (title, source) before resolving to avoid wasting sessions.
        Retries once on miss — catches random flakiness without burning extra sessions.
        """
        unresolved = [d for d in self._candidates if "google.com/search" in d.url]
        if not unresolved:
            return

        seen: set[tuple[str, str]] = set()
        unique_unresolved: list[DealRaw] = []
        for deal in unresolved:
            key = (deal.title.lower().strip(), deal.source.lower().strip())
            if key not in seen:
                seen.add(key)
                unique_unresolved.append(deal)

        logger.info(
            "OrchestratorAgent: resolving %d organic URL(s) (%d duplicates dropped)",
            len(unique_unresolved), len(unresolved) - len(unique_unresolved),
        )

        sem = asyncio.Semaphore(4)

        async def _resolve_one(deal: DealRaw) -> None:
            if not deal.raw_button_label or not deal.search_query:
                logger.debug("_resolve_all_organic: missing identity for %r", deal.title[:40])
                return
            async with sem:
                url = await find_url(
                    self._llm, deal.title, deal.source,
                    deal.raw_button_label, deal.search_query,
                )
                if not url:
                    # Single retry — catches random Browserbase/network flakiness
                    url = await find_url(
                        self._llm, deal.title, deal.source,
                        deal.raw_button_label, deal.search_query,
                    )
                if url:
                    deal.url = url
                    logger.debug("_resolve_all_organic: resolved %r → %s", deal.title[:40], url[:60])
                else:
                    logger.debug("_resolve_all_organic: unresolved after retry: %r", deal.title[:40])

        await asyncio.gather(*(_resolve_one(d) for d in unique_unresolved))

    async def _dispatch(self, tc: Any, keyword: str) -> str:
        try:
            if tc.name == "generate_queries":
                return await self._tool_generate_queries(tc.arguments, keyword)
            elif tc.name == "search_shopping":
                return await self._tool_search_shopping(tc.arguments)
            elif tc.name == "search_web":
                return await self._tool_search_web(tc.arguments)
            elif tc.name == "fetch_page":
                return await self._tool_fetch_page(tc.arguments)
            elif tc.name == "finish":
                return "Research complete."
            else:
                return f"Unknown tool: {tc.name}"
        except Exception as exc:
            logger.warning("OrchestratorAgent: tool %r raised %s", tc.name, exc)
            return f"Tool error: {exc}"

    async def _tool_generate_queries(self, args: dict, keyword: str) -> str:
        kw = args.get("keyword") or keyword
        queries = await QueryGenAgent(llm=self._llm).generate(kw)
        return json.dumps(queries)

    async def _tool_search_shopping(self, args: dict) -> str:
        query = args.get("query", "")
        page = self._browser_session.page
        result = await search_shopping(page, query)
        if not result.text:
            return "No content retrieved — page may have been blocked or returned empty."

        deals = await self._extract_shopping_list(result.text)
        if not deals:
            return f"Searched '{query}' — no deals extracted (CAPTCHA or empty results page)."

        # Cap organics to limit downstream resolution sessions
        organic_count = 0
        for deal in deals:
            if "google.com/search" not in deal.url:
                continue  # featured — already has URL
            deal.search_query = result.query
            if organic_count >= _MAX_ORGANIC_PER_QUERY:
                # Drop excess organics — don't add to candidates at all
                deals = [d for d in deals if "google.com/search" not in d.url or d.search_query]
                break
            idx = (deal.listing_index or 1) - 1
            if 0 <= idx < len(result.button_labels):
                deal.raw_button_label = result.button_labels[idx]
            organic_count += 1

        self._candidates.extend(deals)
        return _deal_summary(deals, f"'{query}'")

    async def _tool_search_web(self, args: dict) -> str:
        query = args.get("query", "")
        try:
            client = BraveSearchClient()
            results = await client.search(query, n=8)
        except Exception as exc:
            return f"Web search failed: {exc}"
        if not results:
            return f"No web results for '{query}'."
        lines = [f"Web search results for '{query}':"]
        for r in results:
            lines.append(f"  • {r.title}: {r.url}")
            if r.description:
                lines.append(f"    {r.description[:120]}")
        return "\n".join(lines)

    async def _tool_fetch_page(self, args: dict) -> str:
        url = args.get("url", "")
        if not url:
            return "No URL provided."
        text = await fetch_page(url)
        if not text:
            return f"Failed to fetch {url}."
        return text[:4_000]

    async def _extract_shopping_list(self, text: str) -> list[DealRaw]:
        messages = [
            {"role": "system", "content": _SHOPPING_EXTRACT_PROMPT},
            {"role": "user", "content": text[:6_000]},
        ]
        try:
            response = await self._llm.complete(messages)
            content = (response.content or "").strip()
            if content.startswith("```"):
                content = content.split("```")[1].lstrip("json").strip()
            data = json.loads(content)
            products = data.get("products", [])
            logger.debug("_extract_shopping_list: LLM returned %d products", len(products))
        except Exception as exc:
            logger.debug("_extract_shopping_list: parse failed: %s", exc)
            return []

        deals = []
        for p in products:
            deal = _product_to_deal_raw(p)
            if deal:
                deals.append(deal)
        return deals


def _product_to_deal_raw(p: dict[str, Any]) -> DealRaw | None:
    try:
        title = str(p.get("title") or "").strip()
        price = float(p.get("price") or 0)
        listed = float(p.get("listed_price") or price)
        merchant = str(p.get("merchant") or "unknown").strip()
        url = str(p.get("url") or "").strip()

        if not title or price <= 0:
            return None
        if listed < price:
            listed = price
        if not url:
            url = f"https://www.google.com/search?q={quote_plus(title)}&tbm=shop"

        condition_str = str(p.get("condition") or "unknown").lower()
        try:
            condition = Condition(condition_str)
        except ValueError:
            condition = Condition.unknown

        raw_index = p.get("listing_index")
        listing_index: int | None = None
        if raw_index is not None:
            try:
                listing_index = int(raw_index)
            except (ValueError, TypeError):
                pass

        return DealRaw(
            source=merchant,
            title=title,
            url=url,
            listed_price=listed,
            sale_price=price,
            condition=condition,
            listing_index=listing_index,
        )
    except (ValueError, TypeError):
        return None


def _assistant_message(response: LLMResponse) -> dict[str, Any]:
    return {
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
    }


def _deal_summary(deals: list[DealRaw], source_label: str) -> str:
    needs_url = [d for d in deals if "google.com/search" in d.url]
    lines = [f"Found {len(deals)} deal(s) from {source_label}:"]
    for d in deals[:_MAX_SUMMARY_DEALS]:
        has_url = "google.com/search" not in d.url
        url_status = "URL ✓" if has_url else "URL pending"
        discount = (
            f" (was ${d.listed_price:.2f}, "
            f"{(d.listed_price - d.sale_price) / d.listed_price * 100:.0f}% off)"
            if d.listed_price > d.sale_price
            else ""
        )
        lines.append(f"  • [{d.source}] {d.title}: ${d.sale_price:.2f}{discount} [{url_status}]")
    if needs_url:
        lines.append(
            f"\n{len(needs_url)} organic listing(s) will have URLs resolved after research."
        )
    return "\n".join(lines)
