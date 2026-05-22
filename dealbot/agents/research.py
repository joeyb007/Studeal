"""ResearchAgent — autonomous ReAct loop that hunts for deals.

Given a WatchlistContext (the user's intent), the agent has a bounded turn
budget. Each turn it:
  1. Reasons about what to search for next
  2. Calls the search tool (which internally does semantic-cache → provider → pool-retrieval)
  3. Observes results, updates state
  4. Decides to search again, refine, or stop

The tool is provider-agnostic. The router decides which providers to call
(or whether to skip them and use cache). The agent reasons about queries,
not providers.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from dealbot.db.database import get_async_session
from dealbot.db.semantic import (
    find_recent_similar_query,
    persist_hunt_query,
    retrieve_similar_deals,
)
from dealbot.llm.base import LLMClient
from dealbot.llm.embeddings import embed_text
from dealbot.schemas import Condition, DealRaw, WatchlistContext
from dealbot.search import SearchResult, SearchRouter

logger = logging.getLogger(__name__)

MAX_TURNS = 8
DEAL_CAP = 30
LAYER1_CACHE_MAX_AGE_HOURS = 24


@dataclass
class ToolObservation:
    """What the agent sees after issuing a search tool call."""

    query: str
    source: str  # "cache" | "external"
    external_count: int = 0  # fresh from provider(s)
    pool_count: int = 0      # pulled from existing Deal pool
    cache_hit: bool = False
    cached_query: str | None = None
    deals_added: int = 0     # new unique deals added to running set
    cost_usd: float = 0.0


@dataclass
class ResearchState:
    """The agent's working memory across turns."""

    context: WatchlistContext
    turns_used: int = 0
    observations: list[ToolObservation] = field(default_factory=list)
    deals_by_url: dict[str, DealRaw] = field(default_factory=dict)
    total_cost_usd: float = 0.0
    stopped: bool = False
    stop_reason: str = ""

    @property
    def turns_remaining(self) -> int:
        return max(0, MAX_TURNS - self.turns_used)

    @property
    def deal_count(self) -> int:
        return len(self.deals_by_url)


_SYSTEM_PROMPT = """\
You are Scout's research agent. The user has described what they're hunting for. \
Your job: find as many relevant deals as possible by issuing focused search queries.

Each turn you have ONE tool:
  search(query: str) — issues a search across multiple providers, returns count of \
  fresh + cached + pool deals merged so far.

You can also call stop() when you're satisfied or have exhausted promising angles.

Strategy:
- First turn: search the user's core product_query with locale/budget context baked in
- Subsequent turns: explore different angles — refurbished, specific brands, retailer-specific, \
  alternative product categories ("gaming laptop" → "RTX 4060 laptop")
- Observe: high pool_count means this area is well-covered; high cache_hit means a similar \
  query was just run by another agent
- Stop when: you have 30+ deals, OR diminishing returns (multiple low-yield turns), OR \
  you've exhausted angles

Hard limits:
- Max 8 turns total. The wrapper will force-stop at the limit.
- Stop early if you have 30+ unique deals.

Always respond with strict JSON, no other text:
{"action": "search", "query": "..."}
OR
{"action": "stop", "reason": "..."}"""


class ResearchAgent:
    def __init__(self, llm: LLMClient, router: SearchRouter | None = None) -> None:
        self._llm = llm
        self._router = router or SearchRouter()

    async def run(
        self,
        watchlist_id: int,
        context: WatchlistContext,
        event_sink: "asyncio.Queue[dict] | None" = None,
    ) -> ResearchState:
        """Run the ReAct loop. Pushes events to event_sink for live streaming if provided."""
        state = ResearchState(context=context)

        async def emit(event_type: str, **payload: Any) -> None:
            if event_sink is not None:
                await event_sink.put({"type": event_type, **payload})

        await emit("research_start",
                   product=context.product_query, max_turns=MAX_TURNS, deal_cap=DEAL_CAP)

        while not state.stopped and state.turns_remaining > 0 and state.deal_count < DEAL_CAP:
            state.turns_used += 1
            await emit("turn_start", turn=state.turns_used, deals_so_far=state.deal_count)

            decision = await self._decide(state)
            if decision.get("action") == "stop":
                state.stopped = True
                state.stop_reason = decision.get("reason", "Agent chose to stop")
                await emit("turn_stop", turn=state.turns_used, reason=state.stop_reason)
                break

            query = (decision.get("query") or "").strip()
            if not query:
                logger.warning("ResearchAgent: empty query in decision, stopping")
                state.stopped = True
                state.stop_reason = "Invalid action"
                break

            await emit("search_start", turn=state.turns_used, query=query)
            obs = await self._execute_search(watchlist_id, query, state)
            state.observations.append(obs)
            state.total_cost_usd += obs.cost_usd

            await emit("search_complete",
                       turn=state.turns_used,
                       query=query,
                       cache_hit=obs.cache_hit,
                       external_count=obs.external_count,
                       pool_count=obs.pool_count,
                       deals_added=obs.deals_added,
                       cost_usd=obs.cost_usd,
                       total_deals=state.deal_count,
                       total_cost=state.total_cost_usd)

        if state.deal_count >= DEAL_CAP:
            state.stop_reason = state.stop_reason or "deal cap reached"
        elif state.turns_remaining == 0:
            state.stop_reason = state.stop_reason or "turn budget exhausted"

        await emit("research_complete",
                   deals=state.deal_count,
                   turns_used=state.turns_used,
                   cost_usd=state.total_cost_usd,
                   reason=state.stop_reason)
        return state

    async def _decide(self, state: ResearchState) -> dict[str, Any]:
        """Ask the LLM what to do next given current state."""
        obs_summary = "\n".join(
            f"  Turn {i+1}: search({o.query!r}) → "
            f"{'CACHE' if o.cache_hit else 'EXTERNAL'} "
            f"fresh={o.external_count} pool={o.pool_count} new_deals={o.deals_added}"
            for i, o in enumerate(state.observations)
        ) or "  (no searches yet)"

        user_msg = (
            f"User intent: {state.context.model_dump_json()}\n"
            f"Turn {state.turns_used} of {MAX_TURNS}.\n"
            f"Deals accumulated: {state.deal_count} / {DEAL_CAP}.\n"
            f"Total cost so far: ${state.total_cost_usd:.4f}\n"
            f"\nObservations:\n{obs_summary}\n"
            f"\nChoose your next action."
        )

        try:
            response = await self._llm.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                response_format={"type": "json_object"},
            )
            return json.loads(response.content or "{}")
        except Exception:
            logger.exception("ResearchAgent._decide: LLM call failed, falling back to stop")
            return {"action": "stop", "reason": "LLM decision failed"}

    async def _execute_search(
        self,
        watchlist_id: int,
        query: str,
        state: ResearchState,
    ) -> ToolObservation:
        """Run the search tool. Internally: Layer 1 cache → provider (if miss) → Layer 2 pool retrieve."""
        embedding = await embed_text(query)

        async with get_async_session() as session:
            # Layer 1 — query→query cache
            cached = await find_recent_similar_query(embedding, session) if embedding else None

            external_results: list[SearchResult] = []
            cost = 0.0
            cache_hit = False
            cached_query_text = None

            if cached is not None:
                cache_hit = True
                cached_query_text = cached.query_text
                # Pull the deals that prior hunt produced (already persisted)
                external_results = [_deal_to_search_result(d) for d in cached.deals]
            else:
                # Cache miss → fire the router (parallel providers)
                results, hunt_cost = await self._router.search(query, locale="ca")
                external_results = results
                cost = hunt_cost.total_usd

            # Layer 2 — query→deal pool (always runs)
            pool_deals = await retrieve_similar_deals(embedding, session) if embedding else []
            pool_results = [_deal_to_search_result(d) for d in pool_deals]

            # Persist a HuntQuery row for future cache hits (only on cache miss)
            deal_ids_to_link: list[int] = []
            if not cache_hit and embedding:
                # We only know deal_ids AFTER scoring/persistence; for now link only existing pool deals
                deal_ids_to_link = [d.id for d in pool_deals]
                await persist_hunt_query(
                    watchlist_id=watchlist_id,
                    query_text=query,
                    embedding=embedding,
                    cost_usd=cost,
                    deal_ids=deal_ids_to_link,
                    session=session,
                )
                await session.commit()

        # Merge into running state (dedupe by URL)
        before = state.deal_count
        for r in external_results + pool_results:
            deal = _search_result_to_deal_raw(r, query)
            if deal is None:
                continue
            if deal.url and deal.url not in state.deals_by_url:
                state.deals_by_url[deal.url] = deal
        added = state.deal_count - before

        return ToolObservation(
            query=query,
            source="cache" if cache_hit else "external",
            external_count=len(external_results),
            pool_count=len(pool_results),
            cache_hit=cache_hit,
            cached_query=cached_query_text,
            deals_added=added,
            cost_usd=cost,
        )


def _deal_to_search_result(d: Any) -> SearchResult:
    """Convert a persisted Deal row to a SearchResult for merging into agent state."""
    return SearchResult(
        title=d.title,
        url=d.url or "",
        snippet="",
        sale_price=d.sale_price,
        listed_price=d.listed_price,
        source_domain=d.source,
        provider="cache",
    )


def _search_result_to_deal_raw(r: SearchResult, search_query: str) -> DealRaw | None:
    if not r.url or not r.title:
        return None
    if r.sale_price is None or r.sale_price <= 0:
        return None

    listed = r.listed_price if r.listed_price and r.listed_price > r.sale_price else r.sale_price
    source_name = r.source_domain.replace("www.", "") if r.source_domain else r.provider

    return DealRaw(
        source=source_name,
        title=r.title,
        url=r.url,
        listed_price=listed,
        sale_price=r.sale_price,
        description=r.snippet[:500] if r.snippet else None,
        condition=Condition.unknown,
        source_type="api",
        search_query=search_query,
    )
