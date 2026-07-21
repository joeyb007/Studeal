"""Composition root for the v14 hunt pipeline.

Single entry point — `run_hunt(spec)` — that:
    1. Builds two LLM clients — nav (AGENT_NAV_MODEL, default gpt-4o) for
       Explorer/MarketplaceRouter, extract (OPENAI_MODEL, default
       gpt-4o-mini) for Extractor/QueryGenerator; Groq fallback for both.
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
import re

from dealbot.agents.explorer import Explorer
from dealbot.agents.extractor_pool import ExtractorPool
from dealbot.agents.marketplace_router import MarketplaceRouter
from dealbot.agents.query_generator import QueryGenerator
from dealbot.agents.tracing import FilesystemTraceWriter, NullTraceWriter, TraceWriter
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

def build_nav_llm() -> LLMClient:
    """Navigator model — the capability-critical path. Frontier by default."""
    if os.environ.get("OPENAI_API_KEY"):
        return OpenAIClient(model=os.environ.get("AGENT_NAV_MODEL", "gpt-4o"))
    return GroqClient(model=_GROQ_70B)


def build_extract_llm() -> LLMClient:
    """Extraction model — high-volume, structured output. Cheap by default."""
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
    nav_llm: LLMClient,
    pool: ExtractorPool,
) -> None:
    """Route the query, open a session, explore each marketplace sequentially.

    Sessions are per-query — one browser context per parallel worker. Within
    a session, marketplaces run sequentially (single browser, single tab).
    Each query gets its own TraceWriter so traces are isolated per worker.
    """
    try:
        targets = await router.route(query, spec)
    except Exception:
        logger.exception("_run_one_query: router failed for %r", query)
        return

    trace_dir = os.environ.get("AGENT_TRACE_DIR")
    if trace_dir:
        from datetime import datetime, timezone
        from pathlib import Path
        slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    else:
        stamp = slug = None  # unused when trace_dir is None

    async def sink(snap, marketplace):
        await pool.submit(snap, marketplace, spec)

    try:
        async with build_session_from_env() as session:
            for target in targets:
                if trace_dir:
                    from pathlib import Path
                    trace: TraceWriter = FilesystemTraceWriter(
                        Path(trace_dir) / f"{stamp}_{slug}" / target.marketplace
                    )
                else:
                    trace = NullTraceWriter()
                explorer = Explorer(nav_llm, trace=trace)
                try:
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
                finally:
                    trace.finalize()
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
    nav_llm = build_nav_llm()
    extract_llm = build_extract_llm()
    extractor = Extractor(extract_llm)
    pool = ExtractorPool(extractor, num_workers=3)
    router = MarketplaceRouter(nav_llm)
    query_gen = QueryGenerator(extract_llm)

    queries = await query_gen.generate(spec)
    logger.info("run_hunt: generated %d queries: %s", len(queries), queries)

    await pool.start()

    # Fan out per-query workers. return_exceptions so one query failing doesn't
    # kill the whole hunt. Each worker builds its own Explorer + TraceWriter.
    await asyncio.gather(
        *(_run_one_query(q, spec, router, nav_llm, pool) for q in queries),
        return_exceptions=True,
    )

    offers = await pool.drain()
    logger.info(
        "run_hunt: %d queries → %d offers collected", len(queries), len(offers),
    )
    return offers
