"""Composition root for the v14 hunt pipeline.

Single entry point — `run_hunt(spec)` — that:
    1. Builds an LLM client (Groq Llama 3.3 70B).
    2. Generates N distinct query phrasings via QueryGenerator (P1).
    3. Routes each query to a subset of curated marketplaces via
       MarketplaceRouter (P2).
    4. Fans out one Explorer per query in parallel — each with its own
       BrowserSession — feeding a shared ExtractorPool.
    5. Drains the pool and returns aggregated Offers.

Backend selection: AGENT_BROWSER_BACKEND=browserbase (default) | local.
"""

from __future__ import annotations

import asyncio
import logging
import os

from dealbot.agents.explorer import Explorer
from dealbot.agents.extractor_pool import ExtractorPool
from dealbot.agents.marketplace_router import MarketplaceRouter
from dealbot.agents.query_generator import QueryGenerator
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.openai_client import OpenAIClient
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
    """Prefer OpenAI when OPENAI_API_KEY is set (better paid-tier RPM);
    fall back to Groq (free tier, tight limits)."""
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient(model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"))
    return GroqClient(model=_GROQ_70B)


def build_session_from_env() -> BrowserSession:
    backend = os.environ.get("AGENT_BROWSER_BACKEND", "browserbase").lower()
    if backend == "local":
        # FB Marketplace requires an authenticated session. If FB_STATE_PATH
        # is set and the file exists, load it — otherwise the session runs
        # without auth and any FB run will hit the login wall.
        storage_state = os.environ.get("FB_STATE_PATH")
        if storage_state:
            from pathlib import Path
            if not Path(storage_state).exists():
                storage_state = None
        return LocalPlaywrightSession(storage_state=storage_state)
    if backend == "browserbase":
        return BrowserbaseSession(proxies=True)
    raise ValueError(
        f"Unknown AGENT_BROWSER_BACKEND: {backend!r}. Expected 'browserbase' or 'local'."
    )


# ---------------------------------------------------------------------------
# Per-query worker (one BrowserSession + N marketplaces)
# ---------------------------------------------------------------------------

async def _run_one_query(
    query: str,
    spec: WatchlistContext,
    router: MarketplaceRouter,
    explorer: Explorer,
    pool: ExtractorPool,
) -> None:
    """Route the query, open a session, explore each marketplace sequentially.

    Sessions are per-query — one browser context per parallel worker. Within
    a session, marketplaces run sequentially (single browser, single tab).
    """
    try:
        targets = await router.route(query, spec)
    except Exception:
        logger.exception("_run_one_query: router failed for %r", query)
        return

    async def sink(snap, marketplace):
        await pool.submit(snap, marketplace, spec)

    try:
        async with build_session_from_env() as session:
            for target in targets:
                try:
                    result = await explorer.explore(
                        entry_url=target.entry_url,
                        marketplace=target.marketplace,
                        query=query,
                        spec=spec,
                        session=session,
                        sink=sink,
                    )
                    logger.info(
                        "run_hunt[%s]: %s urls=%d turns=%d stop=%s",
                        query, target.marketplace,
                        len(result.urls_visited), result.turns_used,
                        result.stop_reason,
                    )
                except Exception:
                    logger.exception(
                        "run_hunt[%s]: explorer failed on %s",
                        query, target.marketplace,
                    )
    except Exception:
        logger.exception("_run_one_query: session setup failed for %r", query)


# ---------------------------------------------------------------------------
# The hunt runner
# ---------------------------------------------------------------------------

async def run_hunt(spec: WatchlistContext) -> list[Offer]:
    """Full v14 hunt pipeline.

    Parallelism = number of queries. Each query gets its own BrowserSession +
    Explorer; all feed a shared ExtractorPool. Extraction happens in parallel
    with continued browsing, bounded by pool worker count.
    """
    llm = build_llm_from_env()
    extractor = Extractor(llm)
    pool = ExtractorPool(extractor, num_workers=3)
    explorer = Explorer(llm)
    router = MarketplaceRouter(llm)
    query_gen = QueryGenerator(llm)

    queries = await query_gen.generate(spec)
    logger.info("run_hunt: generated %d queries: %s", len(queries), queries)

    await pool.start()

    # Fan out per-query workers. return_exceptions so one query failing doesn't
    # kill the whole hunt.
    await asyncio.gather(
        *(_run_one_query(q, spec, router, explorer, pool) for q in queries),
        return_exceptions=True,
    )

    offers = await pool.drain()
    logger.info(
        "run_hunt: %d queries → %d offers collected", len(queries), len(offers),
    )
    return offers
