"""Agent card data surfaces: market analysis + ask-Scout-about-this-market.

GET  /watchlists/{id}/market  deterministic stats over the agent's comp set
POST /watchlists/{id}/ask     stateless Scout Q&A grounded in the playbook,
                              the market numbers, and the buyer profile
"""

from __future__ import annotations

import json
import logging
import os
import statistics
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from dealbot.agents.playbook import sanitize
from dealbot.api.auth import get_current_user
from dealbot.db.database import get_async_session
from dealbot.db.models import User, Watchlist, WatchlistRanking
from dealbot.llm.base import LLMClient
from dealbot.recsys.market_stats import agent_comps, compute_market
from dealbot.schemas import ChatMessage, WatchlistContext

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/watchlists", tags=["market"])

_CHAT_HISTORY_MAX = 30
_TOP_PICKS = 5


def _chat_llm() -> LLMClient:
    backend = os.environ.get("LLM_BACKEND", "openai")
    if backend == "bedrock":
        from dealbot.llm.bedrock_client import DEFAULT_NAV_MODEL, BedrockClient
        return BedrockClient(model=os.environ.get("BEDROCK_SCOUT_MODEL", DEFAULT_NAV_MODEL))
    from dealbot.llm.openai_client import OpenAIClient
    return OpenAIClient()


async def _owned_watchlist(watchlist_id: int, user: User) -> Watchlist:
    async with get_async_session() as session:
        watchlist = await session.get(Watchlist, watchlist_id)
    if watchlist is None or watchlist.user_id != user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Agent not found.")
    return watchlist


async def _pick_ids(watchlist_id: int) -> list[int]:
    from sqlalchemy import select

    async with get_async_session() as session:
        rows = (await session.execute(
            select(WatchlistRanking.listing_id)
            .where(WatchlistRanking.watchlist_id == watchlist_id)
            .order_by(WatchlistRanking.position)
            .limit(_TOP_PICKS)
        )).scalars().all()
    return list(rows)


class MarketResponse(BaseModel):
    n_live: int
    typical: int | None
    band: dict | None
    within_budget: int | None
    ceiling: float | None
    newest_find_hours: float | None
    histogram: list[dict]
    pick_prices: list[dict]
    structure: dict
    heat: dict
    negotiation: dict | None
    going_rate_prose: str | None    # the playbook's market section, if parsed


def _going_rate_section(playbook: str | None) -> str | None:
    """Extract 'The going rate' prose from the playbook's fixed headings."""
    if not playbook:
        return None
    try:
        start = playbook.index("The going rate") + len("The going rate")
        rest = playbook[start:]
        end = rest.index("How to haggle") if "How to haggle" in rest else len(rest)
        text = rest[:end].strip()
        return text or None
    except ValueError:
        return None


@router.get("/{watchlist_id}/market", response_model=MarketResponse)
async def market_analysis(
    watchlist_id: int,
    current_user: User = Depends(get_current_user),
) -> MarketResponse:
    watchlist = await _owned_watchlist(watchlist_id, current_user)
    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    comps = await agent_comps(watchlist)
    picks = await _pick_ids(watchlist_id)
    market = compute_market(comps, context, pick_ids=picks)
    market["going_rate_prose"] = _going_rate_section(watchlist.playbook)
    return MarketResponse(**market)


ASK_SYSTEM = """You are Scout, the user's expert friend on this secondhand market.
You wrote the playbook and computed the numbers below. Answer their question.

You can see the live listings yourself: call view_listings to browse or filter
them, and aggregate_listings for computed stats over any slice. Use them
whenever the question touches specific listings or a slice of the market;
never guess a number a tool can compute.

- Reply in 1-3 sentences, direct, warm, plain language. Numbers over
  adjectives. Only what changes their decision.
- When you mention a specific listing, name it by title and price.
- Ground everything in the playbook, the numbers, the tools, or the buyer
  context. Unknowable from those: say so in one sentence.
- You cannot act from this chat (no sweeps, no contacting anyone); point at
  the right button instead.
- Never use em dashes. No greetings, no stock closers. Plain text only."""


ASK_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "view_listings",
            "description": "Browse the live listings in this market, filtered and sorted.",
            "parameters": {
                "type": "object",
                "properties": {
                    "sort": {"type": "string", "enum": ["price_asc", "price_desc", "newest"]},
                    "min_price": {"type": "number"},
                    "max_price": {"type": "number"},
                    "marketplace": {"type": "string", "description": "e.g. facebook, kijiji, ebay"},
                    "condition": {"type": "string"},
                    "limit": {"type": "integer", "description": "max rows, up to 30"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_listings",
            "description": "Deterministic stats (count, min, median, avg, max price) over the live listings, optionally grouped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_by": {"type": "string", "enum": ["marketplace", "condition", "none"]},
                    "min_price": {"type": "number"},
                    "max_price": {"type": "number"},
                },
            },
        },
    },
]

_TOOL_ROUNDS_MAX = 3
_VIEW_LIMIT_MAX = 30


def _filtered(comps: list, args: dict) -> list:
    out = comps
    if isinstance(args.get("min_price"), (int, float)):
        out = [l for l in out if l.price >= args["min_price"]]
    if isinstance(args.get("max_price"), (int, float)):
        out = [l for l in out if l.price <= args["max_price"]]
    if isinstance(args.get("marketplace"), str) and args["marketplace"].strip():
        needle = args["marketplace"].strip().lower()
        out = [l for l in out if needle in l.marketplace.lower()]
    if isinstance(args.get("condition"), str) and args["condition"].strip():
        needle = args["condition"].strip().lower()
        out = [l for l in out if needle in (l.condition or "").lower()]
    return out


def _run_view(comps: list, args: dict) -> str:
    rows = _filtered(comps, args)
    sort = args.get("sort", "price_asc")
    if sort == "price_desc":
        rows = sorted(rows, key=lambda l: -l.price)
    elif sort == "newest":
        rows = sorted(rows, key=lambda l: l.first_seen_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    else:
        rows = sorted(rows, key=lambda l: l.price)
    limit = args.get("limit")
    limit = min(limit, _VIEW_LIMIT_MAX) if isinstance(limit, int) and limit > 0 else 15
    shown = rows[:limit]
    now = datetime.now(timezone.utc)
    lines = [f"{len(rows)} listings match; showing {len(shown)}."]
    for l in shown:
        age = ""
        if l.first_seen_at:
            hours = max(0, (now - l.first_seen_at).total_seconds() / 3600)
            age = f" · listed {hours / 24:.0f}d ago" if hours >= 48 else f" · listed {max(1, round(hours))}h ago"
        loc = f" · {l.location}" if l.location else ""
        lines.append(f"- {l.title[:80]} · ${l.price:.0f} · {l.marketplace} · {l.condition or 'condition unknown'}{loc}{age}")
    return "\n".join(lines)


def _run_aggregate(comps: list, args: dict) -> str:
    rows = _filtered(comps, args)
    if not rows:
        return "No listings match those filters."

    def stats(group: list) -> str:
        prices = sorted(l.price for l in group)
        return (
            f"count {len(prices)}, min ${prices[0]:.0f}, "
            f"median ${statistics.median(prices):.0f}, "
            f"avg ${statistics.fmean(prices):.0f}, max ${prices[-1]:.0f}"
        )

    group_by = args.get("group_by", "none")
    if group_by not in ("marketplace", "condition"):
        return f"All matching listings: {stats(rows)}"
    groups: dict[str, list] = {}
    for l in rows:
        key = (getattr(l, group_by, None) or "unknown") if group_by != "marketplace" else l.marketplace
        groups.setdefault(key, []).append(l)
    lines = [f"Grouped by {group_by} ({len(rows)} listings):"]
    for key, group in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        lines.append(f"- {key}: {stats(group)}")
    return "\n".join(lines)


def _run_tool(name: str, args: dict, comps: list) -> str:
    try:
        if name == "view_listings":
            return _run_view(comps, args)
        if name == "aggregate_listings":
            return _run_aggregate(comps, args)
        return f"Unknown tool: {name}"
    except Exception:
        logger.exception("ask tool %s failed", name)
        return "Tool failed; answer from the numbers you already have."


class AskRequest(BaseModel):
    messages: list[ChatMessage]


class AskResponse(BaseModel):
    reply: str


@router.post("/{watchlist_id}/ask", response_model=AskResponse)
async def ask_scout(
    watchlist_id: int,
    body: AskRequest,
    current_user: User = Depends(get_current_user),
) -> AskResponse:
    watchlist = await _owned_watchlist(watchlist_id, current_user)
    context = (
        WatchlistContext.model_validate_json(watchlist.context)
        if watchlist.context else None
    )
    comps = await agent_comps(watchlist)
    market = compute_market(comps, context)

    grounding_parts = [f"Category: {context.product_query}" if context else "Category: unknown"]
    if context and context.max_budget:
        grounding_parts.append(f"Buyer budget: ${context.max_budget:.0f}")
    if context and context.buyer_profile:
        grounding_parts.append(f"Buyer profile: {context.buyer_profile}")
    grounding_parts.append("Market numbers (yours, computed): " + json.dumps({
        k: market[k] for k in ("n_live", "typical", "band", "within_budget",
                               "newest_find_hours", "heat", "negotiation", "structure")
    }))
    if watchlist.playbook:
        grounding_parts.append(f"Your playbook:\n{watchlist.playbook}")

    history = [
        {"role": m.role, "content": m.content}
        for m in body.messages[-_CHAT_HISTORY_MAX:]
        if m.role in ("user", "assistant")
    ]
    if not history:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Empty chat.")

    messages: list[dict] = [
        {"role": "system", "content": ASK_SYSTEM},
        {"role": "system", "content": "\n\n".join(grounding_parts)},
        *history,
    ]
    reply = ""
    try:
        llm = _chat_llm()
        for round_i in range(_TOOL_ROUNDS_MAX + 1):
            # Final round runs without tools so the loop always ends in prose.
            use_tools = ASK_TOOLS if round_i < _TOOL_ROUNDS_MAX else None
            response = await llm.complete(messages, tools=use_tools)
            if not response.tool_calls:
                reply = sanitize((response.content or "").strip())
                break
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {"id": tc.id, "type": "function",
                     "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)}}
                    for tc in response.tool_calls
                ],
            })
            for tc in response.tool_calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _run_tool(tc.name, tc.arguments, comps),
                })
    except Exception:
        logger.exception("watchlist ask failed for %d", watchlist_id)
    if not reply:
        reply = "I hit a snag answering that one. Give it another try in a moment."
    return AskResponse(reply=reply)
