from __future__ import annotations

import asyncio
import json
import logging
from typing import Any
from urllib.parse import quote_plus, urlparse

from dealbot.agents.query_gen import QueryGenAgent
from dealbot.llm.base import LLMClient, LLMResponse
from dealbot.schemas import Condition, DealRaw
from dealbot.scrapers.amazon import search_amazon
from dealbot.scrapers.browser_agent import (
    BrowserSession,
    ShoppingResult,
    fetch_page,
    find_url,
    search_shopping,
)
from dealbot.scrapers.ebay import search_ebay
from dealbot.scrapers.walmart import search_walmart
from dealbot.search.brave import BraveSearchClient

logger = logging.getLogger(__name__)

_MAX_ITERATIONS = 12
_MAX_SUMMARY_DEALS = 6
_MAX_ORGANIC_PER_QUERY = 10
_MAX_BRAVE_FETCHES = 4  # cap page fetches from Brave gap-fill

_EDITORIAL_DOMAINS = {
    "wirecutter.com", "cnet.com", "pcmag.com", "techradar.com", "rtings.com",
    "wired.com", "tomsguide.com", "nytimes.com", "theverge.com", "engadget.com",
    "laptopmag.com", "pcworld.com", "digitaltrends.com", "tomshardware.com",
    "notebookcheck.net", "gsmarena.com", "anandtech.com", "reddit.com",
}

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
                "properties": {"keyword": {"type": "string"}},
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
                "properties": {"query": {"type": "string"}},
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
- url: exact URL for featured deals only — omit for organic. Prefer .ca URLs over .com when both are present for the same product (e.g. bestbuy.ca over bestbuy.com, amazon.ca over amazon.com)
- listing_index: integer N for organic listings — omit for featured

Return ONLY valid JSON:
{"products": [{"title": "...", "price": 0.0, "listed_price": 0.0, "merchant": "...", \
"condition": "unknown", "url": "...", "listing_index": null}]}

If no products are found, return: {"products": []}"""

_PAGE_EXTRACT_PROMPT = """\
Extract a single product deal from the page text below.

Return ONLY valid JSON with these fields (omit fields not present):
{
  "title": "product name",
  "price": 99.99,
  "listed_price": 129.99,
  "merchant": "retailer name",
  "condition": "new" | "used" | "refurb" | "unknown"
}

If this is not a product listing page (it's a review, blog, or category page with no clear \
single product and price), return: {"products": null}"""


class OrchestratorAgent:
    """
    Parallel multi-source deal hunter:
    1. Google Shopping browser agent + API scrapers run in parallel
    2. Results deduplicated by URL + ASIN
    3. Brave search fills gaps — only unique URLs not in pool get fetched
    """

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm
        self._google_candidates: list[DealRaw] = []
        self._browser_session: BrowserSession | None = None

    async def run(self, keyword: str) -> list[DealRaw]:
        # Phase 1: Google Shopping + all API scrapers in parallel
        google_task = self._run_google_loop(keyword)
        api_task = self._run_api_scrapers(keyword)

        google_results, api_results = await asyncio.gather(
            google_task, api_task, return_exceptions=True
        )

        if isinstance(google_results, Exception):
            logger.warning("OrchestratorAgent: Google Shopping failed: %s", google_results)
            google_results = []
        if isinstance(api_results, Exception):
            logger.warning("OrchestratorAgent: API scrapers failed: %s", api_results)
            api_results = []

        # Resolve organic Google Shopping URLs before deduping
        self._google_candidates = list(google_results)
        await self._resolve_all_organic()

        pool = _dedup(self._google_candidates + list(api_results))
        logger.info("OrchestratorAgent: %d candidates after dedup (Google+APIs)", len(pool))

        # Phase 2: Brave gap-fill — fetch only unique leads not in pool
        brave_candidates = await self._brave_gap_fill(keyword, pool)
        if brave_candidates:
            pool = _dedup(pool + brave_candidates)
            logger.info("OrchestratorAgent: %d candidates after Brave gap-fill", len(pool))

        logger.info("OrchestratorAgent: %d final candidates for %r", len(pool), keyword)
        return pool

    async def _run_google_loop(self, keyword: str) -> list[DealRaw]:
        """Run the existing ReAct browser loop for Google Shopping."""
        self._google_candidates = []

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
        return self._google_candidates

    async def _run_api_scrapers(self, keyword: str) -> list[DealRaw]:
        """Run all retailer API scrapers in parallel."""
        results = await asyncio.gather(
            search_amazon(keyword),
            search_ebay(keyword),
            search_walmart(keyword),
            return_exceptions=True,
        )
        candidates: list[DealRaw] = []
        names = ["Amazon", "eBay", "Walmart"]
        for name, result in zip(names, results):
            if isinstance(result, Exception):
                logger.warning("OrchestratorAgent: %s scraper failed: %s", name, result)
            elif isinstance(result, list):
                # Mark as API-sourced
                for deal in result:
                    deal.source_type = "api"
                candidates.extend(result)
                logger.info("OrchestratorAgent: %s returned %d deals", name, len(result))
        return candidates

    async def _brave_gap_fill(self, keyword: str, pool: list[DealRaw]) -> list[DealRaw]:
        """Brave search → filter to unique URLs not in pool → fetch + extract."""
        pool_urls = {d.url for d in pool if d.url and "google.com/search" not in d.url}
        pool_domains = {_domain(u) for u in pool_urls}

        try:
            client = BraveSearchClient()
            results = await client.search(f"{keyword} deal buy", n=12)
        except Exception as exc:
            logger.warning("OrchestratorAgent: Brave search failed: %s", exc)
            return []

        unique_urls: list[str] = []
        for r in results:
            dom = _domain(r.url)
            if dom in _EDITORIAL_DOMAINS:
                continue
            if r.url in pool_urls:
                continue
            if dom in pool_domains:
                continue  # already have a deal from this retailer
            unique_urls.append(r.url)

        if not unique_urls:
            return []

        logger.info("OrchestratorAgent: Brave gap-fill — %d unique URLs to fetch", len(unique_urls[:_MAX_BRAVE_FETCHES]))

        sem = asyncio.Semaphore(2)

        async def _fetch_and_extract(url: str) -> DealRaw | None:
            async with sem:
                text = await fetch_page(url)
                if not text:
                    return None
                return await self._extract_from_page(url, text)

        tasks = [_fetch_and_extract(u) for u in unique_urls[:_MAX_BRAVE_FETCHES]]
        results_raw = await asyncio.gather(*tasks, return_exceptions=True)

        deals = []
        for r in results_raw:
            if isinstance(r, DealRaw):
                deals.append(r)
        return deals

    async def _extract_from_page(self, url: str, text: str) -> DealRaw | None:
        """Extract a single DealRaw from a fetched page using the LLM."""
        domain = _domain(url)
        messages = [
            {"role": "system", "content": _PAGE_EXTRACT_PROMPT},
            {"role": "user", "content": text[:4_000]},
        ]
        try:
            response = await self._llm.complete(messages)
            content = (response.content or "").strip()
            if content.startswith("```"):
                content = content.split("```")[1].lstrip("json").strip()
            data = json.loads(content)
            if data.get("products") is None:
                return None
            p = data if "title" in data else data.get("products", {})
            if not p or not isinstance(p, dict):
                return None
            price = float(p.get("price") or 0)
            if price <= 0:
                return None
            listed = float(p.get("listed_price") or price)
            if listed < price:
                listed = price
            return DealRaw(
                source=p.get("merchant") or domain,
                title=str(p.get("title") or "").strip(),
                url=url,
                listed_price=listed,
                sale_price=price,
                condition=Condition(p.get("condition", "unknown")) if p.get("condition") in ("new", "used", "refurb", "unknown") else Condition.unknown,
                source_type="scraped",
            )
        except Exception:
            return None

    async def _resolve_all_organic(self) -> None:
        unresolved = [d for d in self._google_candidates if d.url and "google.com/search" in d.url]
        if not unresolved:
            return

        seen: set[tuple[str, str]] = set()
        unique: list[DealRaw] = []
        for deal in unresolved:
            key = (deal.title.lower().strip(), deal.source.lower().strip())
            if key not in seen:
                seen.add(key)
                unique.append(deal)

        logger.info(
            "OrchestratorAgent: resolving %d organic URL(s) (%d duplicates dropped)",
            len(unique), len(unresolved) - len(unique),
        )

        sem = asyncio.Semaphore(4)

        async def _resolve_one(deal: DealRaw) -> None:
            if not deal.raw_button_label or not deal.search_query:
                return
            async with sem:
                url = await find_url(
                    self._llm, deal.title, deal.source,
                    deal.raw_button_label, deal.search_query,
                )
                if not url:
                    url = await find_url(
                        self._llm, deal.title, deal.source,
                        deal.raw_button_label, deal.search_query,
                    )
                if url:
                    deal.url = url

        await asyncio.gather(*(_resolve_one(d) for d in unique))

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

        organic_count = 0
        for deal in deals:
            if "google.com/search" not in (deal.url or ""):
                continue
            deal.search_query = result.query
            if organic_count >= _MAX_ORGANIC_PER_QUERY:
                deals = [d for d in deals if "google.com/search" not in (d.url or "") or d.search_query]
                break
            idx = (deal.listing_index or 1) - 1
            if 0 <= idx < len(result.button_labels):
                deal.raw_button_label = result.button_labels[idx]
            organic_count += 1

        self._google_candidates.extend(deals)
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
        except Exception as exc:
            logger.debug("_extract_shopping_list: parse failed: %s", exc)
            return []

        return [d for p in products if (d := _product_to_deal_raw(p)) is not None]


def _dedup(candidates: list[DealRaw]) -> list[DealRaw]:
    """Deduplicate by URL (exact) and ASIN. Google placeholder URLs are not dedup keys."""
    seen_urls: set[str] = set()
    seen_asins: set[str] = set()
    result: list[DealRaw] = []
    for d in candidates:
        if d.asin and d.asin in seen_asins:
            continue
        url_key = d.url if d.url and "google.com/search" not in d.url else None
        if url_key and url_key in seen_urls:
            continue
        if d.asin:
            seen_asins.add(d.asin)
        if url_key:
            seen_urls.add(url_key)
        result.append(d)
    return result


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower().lstrip("www.")
    except Exception:
        return ""


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
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in response.tool_calls
        ],
    }


def _deal_summary(deals: list[DealRaw], source_label: str) -> str:
    needs_url = [d for d in deals if d.url and "google.com/search" in d.url]
    lines = [f"Found {len(deals)} deal(s) from {source_label}:"]
    for d in deals[:_MAX_SUMMARY_DEALS]:
        has_url = not d.url or "google.com/search" not in d.url
        url_status = "URL ✓" if has_url else "URL pending"
        discount = (
            f" (was ${d.listed_price:.2f}, "
            f"{(d.listed_price - d.sale_price) / d.listed_price * 100:.0f}% off)"
            if d.listed_price > d.sale_price else ""
        )
        lines.append(f"  • [{d.source}] {d.title}: ${d.sale_price:.2f}{discount} [{url_status}]")
    if needs_url:
        lines.append(f"\n{len(needs_url)} organic listing(s) will have URLs resolved after research.")
    return "\n".join(lines)
