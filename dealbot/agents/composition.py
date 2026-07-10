"""Composition root for the v14 hunt pipeline.

Single entry point — `run_hunt(spec)` — that:
    1. Builds an LLM client (Groq Llama 3.3 70B).
    2. Builds an Extractor + ExtractorPool + Explorer.
    3. Opens a BrowserSession (Browserbase in prod, LocalPlaywright in dev/eval).
    4. Derives start URLs from the spec.
    5. Runs one Explorer per (marketplace, start_url).
    6. Drains the pool and returns aggregated Offers.

Day 2 scope: sequential per-marketplace exploration. Day 3 adds parallel
per-query Explorers + a real MarketplaceRouter that replaces the hardcoded
start-URL derivation.

Backend selection: AGENT_BROWSER_BACKEND=browserbase (default) | local.
"""

from __future__ import annotations

import logging
import os

from dealbot.agents.explorer import Explorer
from dealbot.agents.extractor_pool import ExtractorPool
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import (
    BrowserSession,
    BrowserbaseSession,
    LocalPlaywrightSession,
)

logger = logging.getLogger(__name__)


_GROQ_70B = "llama-3.3-70b-versatile"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_llm_from_env() -> LLMClient:
    """Return an LLMClient from env config. Groq for now; swap points for
    OpenAI / Anthropic can be added when needed by role."""
    return GroqClient(model=_GROQ_70B)


def build_session_from_env() -> BrowserSession:
    """Backend selection from AGENT_BROWSER_BACKEND."""
    backend = os.environ.get("AGENT_BROWSER_BACKEND", "browserbase").lower()
    if backend == "local":
        return LocalPlaywrightSession()
    if backend == "browserbase":
        return BrowserbaseSession(proxies=True)
    raise ValueError(
        f"Unknown AGENT_BROWSER_BACKEND: {backend!r}. Expected 'browserbase' or 'local'."
    )


def build_start_urls(spec: WatchlistContext) -> list[tuple[str, str]]:
    """(marketplace, search_url) tuples for the hunt.

    Day 2: minimal placeholder — Kijiji only, constructed from `product_query`.
    Day 3 replaces this with the LLM-driven MarketplaceRouter (P2) that picks
    from a curated list per query.
    """
    q_slug = spec.product_query.replace(" ", "-").lower()
    return [
        ("kijiji", f"https://www.kijiji.ca/b-buy-sell/{q_slug}/k0c10"),
    ]


# ---------------------------------------------------------------------------
# The hunt runner
# ---------------------------------------------------------------------------

async def run_hunt(spec: WatchlistContext) -> list[Offer]:
    """Execute the full v14 pipeline for a single watchlist spec.

    Sequential across marketplaces (Day 2). Day 3 wraps this in per-query
    parallel Explorers via asyncio.gather.
    """
    llm = build_llm_from_env()
    extractor = Extractor(llm)
    pool = ExtractorPool(extractor, num_workers=3)
    explorer = Explorer(llm)

    await pool.start()

    async def sink(snap, marketplace):
        await pool.submit(snap, marketplace, spec)

    async with build_session_from_env() as session:
        for marketplace, start_url in build_start_urls(spec):
            try:
                result = await explorer.explore(
                    start_url=start_url,
                    marketplace=marketplace,
                    spec=spec,
                    session=session,
                    sink=sink,
                )
                logger.info(
                    "run_hunt: %s explored: urls=%d turns=%d stop=%s",
                    marketplace, len(result.urls_visited),
                    result.turns_used, result.stop_reason,
                )
            except Exception as exc:
                logger.exception(
                    "run_hunt: explorer for %s failed: %s", marketplace, exc,
                )

    offers = await pool.drain()
    logger.info("run_hunt: collected %d offers total", len(offers))
    return offers
