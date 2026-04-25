from __future__ import annotations

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
    find_url,
    search_shopping,
)

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 12
_MAX_SUMMARY_DEALS = 6

_SYSTEM_PROMPT = """\
You are a deal-hunting research agent for students and budget-conscious shoppers.

Your goal is to find real discounted product listings for the given keyword.

Strategy:
1. Call generate_queries to get focused search variants
2. Call search_shopping with your best 2-3 queries
3. Call finish when you have searched enough queries

URL resolution for organic listings is handled automatically after you finish — \
you do not need to call find_url.

Only extract prices that are explicitly shown — never estimate or invent prices."""

_TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "generate_queries",
            "description": "Generate focused search query variants for a keyword.",
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
                "Search Google Shopping. Returns deal listings with titles, prices, "
                "merchants, and conditions."
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
  - Do NOT assign a featured URL to an organic listing title
  - Do NOT set listing_index for featured deals

ORGANIC LISTINGS — numbered lines with title/price/merchant/condition:
  Format: "N. [DISCOUNT%] TITLE Current Price: $sale_price[. Was $listed_price][. Merchant][. Condition]"
  Rules:
  - Each organic listing is its own product entry
  - Set listing_index to the number N shown at the start of the line (e.g. 1, 2, 3...)
  - No url field — omit it entirely
  - Condition: Pre-owned → used, Refurbished → refurb, explicit New → new, else unknown

For each product extract:
- title: product name and model
- price: sale price as float (USD)
- listed_price: original price as float — same as price if no discount
- merchant: retailer name
- condition: "new" | "used" | "refurb" | "unknown"
- url: exact URL for featured deals only — omit for organic listings
- listing_index: integer N from the organic listing line number — omit for featured deals

Return ONLY valid JSON:
{"products": [{"title": "...", "price": 0.0, "listed_price": 0.0, "merchant": "...", \
"condition": "unknown", "url": "...", "listing_index": null}]}

If no products are found, return: {"products": []}"""


class OrchestratorAgent:
    """
    ReAct orchestrator that owns a single browser session for the entire run.

    Tools:
    - generate_queries: LLM generates search query variants
    - search_shopping: navigates Google Shopping, extracts deals via aria snapshot
    - find_url: LLM subagent navigates the shared browser to resolve retailer URLs
    - fetch_page: fetches a specific URL for detail extraction
    - finish: signals completion

    The browser session is created once at the start of run() and shared
    across search_shopping and find_url calls. No session churn.
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._candidates: list[DealRaw] = []
        self._browser_session: BrowserSession | None = None

    async def run(self, keyword: str) -> list[DealRaw]:
        self._candidates = []

        # Phase 1: LLM orchestrator — search and collect candidates.
        # Single shared BrowserSession for all search_shopping calls only.
        # Resolution (Phase 2) uses independent per-call sessions so it runs
        # outside this context and doesn't compete for the same browser.
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

        # Phase 2: resolve organic URLs — each find_url call opens its own fresh
        # Browserbase session with a proxy, so Google sees each click as a new user.
        await self._resolve_all_organic()

        logger.info("OrchestratorAgent: %d candidates for %r", len(self._candidates), keyword)
        return self._candidates

    async def _resolve_all_organic(self) -> None:
        """
        Post-loop: resolve every candidate still holding a google.com placeholder URL.
        Each DealRaw carries its own raw_button_label and search_query so no lookup
        dict is needed — identity was attached at extraction time.

        Deduplicates by (title, source) before resolving — multiple search queries
        often surface the same product, and resolving duplicates wastes browser sessions
        and burns Google's per-session throttle threshold.
        """
        unresolved = [
            d for d in self._candidates if "google.com/search" in d.url
        ]
        if not unresolved:
            return

        # Deduplicate by (title, source) — keep first occurrence, drop the rest.
        seen: set[tuple[str, str]] = set()
        unique_unresolved: list[DealRaw] = []
        for deal in unresolved:
            key = (deal.title.lower().strip(), deal.source.lower().strip())
            if key not in seen:
                seen.add(key)
                unique_unresolved.append(deal)

        duplicates_dropped = len(unresolved) - len(unique_unresolved)
        logger.info(
            "OrchestratorAgent: resolving %d organic URL(s) (%d duplicates dropped)",
            len(unique_unresolved), duplicates_dropped,
        )

        for deal in unique_unresolved:
            if not deal.raw_button_label or not deal.search_query:
                logger.debug("_resolve_all_organic: missing identity for %r", deal.title[:40])
                continue

            url = await find_url(self._llm, deal.title, deal.source,
                                 deal.raw_button_label, deal.search_query)
            if url:
                deal.url = url
                logger.debug("_resolve_all_organic: resolved %r → %s", deal.title[:40], url[:60])

    async def _dispatch(self, tc: Any, keyword: str) -> str:
        try:
            if tc.name == "generate_queries":
                return await self._tool_generate_queries(tc.arguments, keyword)
            elif tc.name == "search_shopping":
                return await self._tool_search_shopping(tc.arguments)
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

        # Attach identity to each organic deal at extraction time.
        #
        # Pass 1 — primary: listing_index from LLM maps directly to button_labels[i].
        # Both lists are built from the same aria snapshot scan, so the ordering is stable.
        # The LLM sees numbered lines and we ask it to echo the number back; this avoids
        # any cross-array index drift caused by the LLM dropping or reordering listings.
        claimed: set[int] = set()  # 0-based indices already assigned
        for deal in deals:
            if "google.com/search" not in deal.url:
                continue
            deal.search_query = result.query  # always set for all organic deals
            if deal.listing_index is None:
                continue
            idx = deal.listing_index - 1  # 1-based → 0-based
            if 0 <= idx < len(result.button_labels):
                deal.raw_button_label = result.button_labels[idx]
                claimed.add(idx)
                logger.debug(
                    "_tool_search_shopping: attached label[%d] to %r",
                    deal.listing_index, deal.title[:40],
                )
            else:
                logger.debug(
                    "_tool_search_shopping: listing_index %d out of range (%d labels) for %r",
                    deal.listing_index, len(result.button_labels), deal.title[:40],
                )

        # Pass 2 — fallback: for organics where listing_index was missing or invalid,
        # try to match against unclaimed button labels using two heuristics:
        #   A) merchant name + price substring in the raw button text
        #   B) model token overlap (≥2 tokens) as last resort
        # We intentionally use unclaimed labels only to avoid double-assignment.
        unclaimed_labels = [
            (i, lbl) for i, lbl in enumerate(result.button_labels) if i not in claimed
        ]
        for deal in deals:
            if "google.com/search" not in deal.url or deal.raw_button_label is not None:
                continue
            price_str = f"${deal.sale_price:.2f}"
            merchant_lower = deal.source.lower()
            # Heuristic A: merchant name AND price both appear in the button label
            for i, lbl in unclaimed_labels:
                if merchant_lower in lbl.lower() and price_str in lbl:
                    deal.raw_button_label = lbl
                    claimed.add(i)
                    unclaimed_labels = [(j, l) for j, l in unclaimed_labels if j != i]
                    logger.debug(
                        "_tool_search_shopping: fallback-A matched %r → label[%d]",
                        deal.title[:40], i,
                    )
                    break
            if deal.raw_button_label is not None:
                continue
            # Heuristic B: token overlap — pick unclaimed label with ≥2 shared tokens
            title_tokens = set(deal.title.lower().split())
            best_i, best_lbl, best_overlap = -1, "", 0
            for i, lbl in unclaimed_labels:
                overlap = len(title_tokens & set(lbl.lower().split()))
                if overlap > best_overlap:
                    best_i, best_lbl, best_overlap = i, lbl, overlap
            if best_overlap >= 2:
                deal.raw_button_label = best_lbl
                claimed.add(best_i)
                unclaimed_labels = [(j, l) for j, l in unclaimed_labels if j != best_i]
                logger.debug(
                    "_tool_search_shopping: fallback-B matched %r → label[%d] (overlap=%d)",
                    deal.title[:40], best_i, best_overlap,
                )
            else:
                logger.debug(
                    "_tool_search_shopping: no label found for %r", deal.title[:40],
                )

        self._candidates.extend(deals)
        return _deal_summary(deals, f"'{query}'")


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
            for p in products[:3]:
                logger.debug("  product sample: %s", p)
        except Exception as exc:
            logger.debug("OrchestratorAgent._extract_shopping_list: parse failed: %s", exc)
            return []

        deals = []
        for p in products:
            deal = _product_to_deal_raw(p)
            if deal:
                deals.append(deal)
        return deals


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
            f"\n{len(needs_url)} organic listing(s) have placeholder URLs — "
            "they will be resolved automatically after research is complete."
        )
    return "\n".join(lines)


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
