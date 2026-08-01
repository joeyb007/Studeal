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
from dataclasses import dataclass

from dealbot.agents.explorer import Explorer
from dealbot.agents.extractor_pool import ExtractorPool
from dealbot.agents.marketplace_router import MarketplaceRouter
from dealbot.agents.query_generator import QueryGenerator
from dealbot.agents.tracing import (
    FilesystemTraceWriter,
    NullTraceWriter,
    PublishingTraceWriter,
    TraceWriter,
)
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.events.publisher import RedisEventPublisher
from dealbot.events.schema import ExtractionSubmitted, QueriesPlanned
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


@dataclass
class HuntEventContext:
    """Identity + publisher for the live event stream of one hunt.
    Optional everywhere it appears — omitted means no events (eval harness,
    manual trigger paths are unchanged)."""

    publisher: RedisEventPublisher
    hunt_id: int
    watchlist_id: int


# ---------------------------------------------------------------------------
# Throttling
# ---------------------------------------------------------------------------

class ThrottledLLM(LLMClient):
    """Caps concurrent complete() calls across all users of one client.

    3 parallel navigators on gpt-4o saturate the org TPM cap (446k/450k
    observed, run 3) — a shared semaphore trades a little parallelism for
    zero 429 retry-exhaustion deaths.
    """

    def __init__(self, inner: LLMClient, max_concurrency: int) -> None:
        self._inner = inner
        self._sem = asyncio.Semaphore(max_concurrency)

    @property
    def inner(self) -> LLMClient:
        """Read-only access to wrapped LLMClient."""
        return self._inner

    @property
    def supports_vision(self) -> bool:  # type: ignore[override]
        return getattr(self._inner, "supports_vision", False)

    @property
    def total_prompt_tokens(self) -> int:
        return getattr(self._inner, "total_prompt_tokens", 0)

    @property
    def total_completion_tokens(self) -> int:
        return getattr(self._inner, "total_completion_tokens", 0)

    @property
    def call_count(self) -> int:
        return getattr(self._inner, "call_count", 0)

    async def complete(self, messages, tools=None, response_format=None):
        async with self._sem:
            return await self._inner.complete(
                messages, tools=tools, response_format=response_format,
            )


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_nav_llm() -> LLMClient:
    """Navigator model — frontier by default, concurrency-capped.

    LLM_BACKEND=bedrock wins explicitly; otherwise key-presence rules as
    before (OpenAI if keyed, Groq fallback)."""
    limit = int(os.environ.get("AGENT_NAV_CONCURRENCY", "2"))
    if os.environ.get("LLM_BACKEND") == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient

        inner: LLMClient = BedrockClient(
            model=os.environ.get("BEDROCK_NAV_MODEL", DEFAULT_NAV_MODEL))
    elif os.environ.get("OPENAI_API_KEY"):
        inner = OpenAIClient(model=os.environ.get("AGENT_NAV_MODEL", "gpt-4o"))
    else:
        inner = GroqClient(model=_GROQ_70B)
    return ThrottledLLM(inner, max_concurrency=limit)


def build_extract_llm() -> LLMClient:
    """Extraction model — high-volume, structured output. Cheap by default."""
    if os.environ.get("LLM_BACKEND") == "bedrock":
        from dealbot.llm.bedrock_client import BedrockClient

        return BedrockClient(model=os.environ.get("BEDROCK_EXTRACT_MODEL"))
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
    events: HuntEventContext | None = None,
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
        if events is not None:
            events.publisher.publish_nowait(ExtractionSubmitted(
                hunt_id=events.hunt_id, watchlist_id=events.watchlist_id,
                query=query, marketplace=marketplace,
            ))
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
                if events is not None:
                    trace = PublishingTraceWriter(
                        trace, events.publisher,
                        hunt_id=events.hunt_id,
                        watchlist_id=events.watchlist_id,
                        query=query,
                        marketplace=target.marketplace,
                    )
                explorer = Explorer(nav_llm, trace=trace)
                try:
                    try:
                        result = await explorer.explore(
                            entry_url=target.entry_url,
                            entry_referer=target.entry_referer,
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
                        # No-results broadening: verbose query variants can
                        # legitimately match nothing on thin marketplaces
                        # (craigslist AND-matches every term). One retry with
                        # the spec's core product query, re-routed so the
                        # entry URL is rebuilt for the simplified query.
                        if (
                            result.stop_reason == "done"
                            and result.done_reason
                            and "no_results" in result.done_reason.lower()
                            and query.strip().lower() != spec.product_query.strip().lower()
                        ):
                            retry_query = spec.product_query
                            retry_targets = await router.route(retry_query, spec)
                            retry_target = next(
                                (t for t in retry_targets
                                 if t.marketplace == target.marketplace),
                                None,
                            )
                            if retry_target is not None:
                                logger.info(
                                    "run_hunt[%s]: %s no_results — retrying with core query %r",
                                    query, target.marketplace, retry_query,
                                )
                                retry_explorer = Explorer(nav_llm, trace=trace)
                                await retry_explorer.explore(
                                    entry_url=retry_target.entry_url,
                                    entry_referer=retry_target.entry_referer,
                                    marketplace=retry_target.marketplace,
                                    query=retry_query,
                                    spec=spec,
                                    session=session,
                                    sink=sink,
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

async def run_hunt(
    spec: WatchlistContext,
    events: HuntEventContext | None = None,
) -> list[Offer]:
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
    if events is not None:
        await events.publisher.publish(QueriesPlanned(
            hunt_id=events.hunt_id, watchlist_id=events.watchlist_id,
            queries=queries,
        ))

    await pool.start()

    # Fan out per-query workers. return_exceptions so one query failing doesn't
    # kill the whole hunt. Each worker builds its own Explorer + TraceWriter.
    await asyncio.gather(
        *(_run_one_query(q, spec, router, nav_llm, pool, events) for q in queries),
        return_exceptions=True,
    )

    offers = await pool.drain()
    logger.info(
        "run_hunt: %d queries → %d offers collected", len(queries), len(offers),
    )
    return offers
