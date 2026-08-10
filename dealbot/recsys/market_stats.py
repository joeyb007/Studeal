"""Agent-scoped market statistics for the My Agents card.

Every number here is deterministic arithmetic over the agent's comp set and
is computable on the agent's FIRST hunt (universality rule from the
2026-08-09 redesign spec). Nothing model-generated ships from this module.

Comp set rule (reliability): the agent's curated matches (ranker-approved,
score above the weak threshold) when there are enough of them; otherwise
cosine neighbors above the gate's similarity floor. Below MIN_COMPS the
band-derived stats are omitted entirely — an honest absence beats a
fabricated rate. Chips/histogram/negotiation numbers all derive from this
one comp set so the card can never disagree with itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from dealbot.agents.playbook import price_band
from dealbot.db.database import get_async_session
from dealbot.db.models import Listing, Watchlist, WatchlistRanking
from dealbot.lifecycle import stale_cutoff
from dealbot.recsys.gate import GATE_SIMILARITY_TAU
from dealbot.schemas import WatchlistContext

MIN_COMPS = 5
WEAK_SCORE = 0.4          # matches at/above this are "curated"
FAIR_FRACTION = 0.12      # ± around median reads as "fair"
HISTOGRAM_BUCKETS = 8
_NEIGHBOR_LIMIT = 60


# ---------------------------------------------------------------------------
# Comp set
# ---------------------------------------------------------------------------

async def agent_comps(watchlist: Watchlist) -> list[Listing]:
    """Curated matches first; similarity-floor neighbors as fallback."""
    async with get_async_session() as session:
        curated = list((await session.execute(
            select(Listing)
            .join(WatchlistRanking, WatchlistRanking.listing_id == Listing.id)
            .where(WatchlistRanking.watchlist_id == watchlist.id)
            .where(WatchlistRanking.score >= WEAK_SCORE)
            .where(Listing.last_seen_at >= stale_cutoff())
            .where(Listing.sold_at.is_(None))
            .order_by(WatchlistRanking.position)
        )).scalars().all())
        if len(curated) >= MIN_COMPS:
            return curated

        if watchlist.intent_embedding is None:
            return curated
        distance_cutoff = 1.0 - GATE_SIMILARITY_TAU
        neighbors = list((await session.execute(
            select(Listing)
            .where(Listing.embedding.isnot(None))
            .where(Listing.last_seen_at >= stale_cutoff())
            .where(Listing.sold_at.is_(None))
            .where(Listing.embedding.cosine_distance(watchlist.intent_embedding) < distance_cutoff)
            .order_by(Listing.embedding.cosine_distance(watchlist.intent_embedding))
            .limit(_NEIGHBOR_LIMIT)
        )).scalars().all())
        return neighbors if len(neighbors) > len(curated) else curated


# ---------------------------------------------------------------------------
# Pure stats (unit-tested without a DB)
# ---------------------------------------------------------------------------

def price_read(price: float, prices: list[float]) -> dict[str, str] | None:
    """Deterministic price-vs-rate badge. None below MIN_COMPS."""
    band = price_band(prices)
    if band is None:
        return None
    _, median, _ = band
    delta = price - median
    if abs(delta) <= FAIR_FRACTION * median:
        return {"level": "fair", "text": "fair for the market"}
    if delta > 0:
        return {"level": "over", "text": f"about ${delta:.0f} over the going rate"}
    return {"level": "under", "text": f"about ${-delta:.0f} under the going rate"}


def negotiation_numbers(prices: list[float], ceiling: float | None) -> dict[str, float] | None:
    """open / fair_low / fair_high / walk — formulas, never model-invented.
    None without a band (playbook prose carries the tab alone then)."""
    band = price_band(prices)
    if band is None:
        return None
    _, median, p75 = band
    walk = min(ceiling, p75) if ceiling else p75
    return {
        "open": round(0.8 * median),
        "fair_low": round(0.9 * median),
        "fair_high": round(1.05 * median),
        "walk": round(walk),
        "median": round(median),
    }


def histogram(prices: list[float], buckets: int = HISTOGRAM_BUCKETS) -> list[dict[str, float]]:
    """Equal-width buckets over the observed range: [{lo, hi, count}]."""
    if len(prices) < MIN_COMPS:
        return []
    lo, hi = min(prices), max(prices)
    if hi <= lo:
        return [{"lo": lo, "hi": hi, "count": len(prices)}]
    width = (hi - lo) / buckets
    out = [{"lo": lo + i * width, "hi": lo + (i + 1) * width, "count": 0} for i in range(buckets)]
    for p in prices:
        idx = min(int((p - lo) / width), buckets - 1)
        out[idx]["count"] += 1
    return out


@dataclass
class StructureRow:
    label: str
    avg_price: float
    count: int


def price_structure(comps: list[Listing]) -> tuple[str, list[StructureRow]]:
    """Fallback ladder: condition split -> marketplace split -> quartiles.
    (Variant clustering upgrades the top tier later.) Always returns
    something structurally true for >= MIN_COMPS comps; ("none", []) below."""
    if len(comps) < MIN_COMPS:
        return ("none", [])

    def _split(keyfn) -> list[StructureRow]:
        groups: dict[str, list[float]] = {}
        for c in comps:
            key = keyfn(c)
            if key:
                groups.setdefault(key, []).append(c.price)
        rows = [
            StructureRow(label=k, avg_price=sum(v) / len(v), count=len(v))
            for k, v in groups.items() if len(v) >= 3
        ]
        return sorted(rows, key=lambda r: -r.avg_price) if len(rows) >= 2 else []

    cond = _split(lambda c: c.condition if c.condition and c.condition != "unknown" else None)
    if cond:
        return ("condition", cond)
    market = _split(lambda c: c.marketplace)
    if market:
        return ("marketplace", market)

    prices = sorted(c.price for c in comps)
    n = len(prices)
    quart = [
        StructureRow("cheapest quarter", sum(prices[: n // 4 or 1]) / (n // 4 or 1), n // 4 or 1),
        StructureRow("priciest quarter", sum(prices[-(n // 4 or 1):]) / (n // 4 or 1), n // 4 or 1),
    ]
    return ("quartiles", quart)


def market_heat(
    n_live: int, within_budget: int | None, newest_age_hours: float | None,
) -> dict[str, str]:
    """Day-one heat formula: supply, budget coverage, freshness. Velocity and
    price drift join the formula once history accrues (spec upgrade path)."""
    supply_pts = min(n_live / 10.0, 1.0)
    coverage_pts = (within_budget / n_live) if (within_budget is not None and n_live) else 0.5
    fresh_pts = 0.0
    if newest_age_hours is not None:
        fresh_pts = 1.0 if newest_age_hours <= 24 else 0.5 if newest_age_hours <= 72 else 0.0
    score = (supply_pts + coverage_pts + fresh_pts) / 3.0

    if score >= 0.62:
        return {"level": "good", "label": "buyer's market right now",
                "why": "plenty of supply, most of it inside your budget, fresh listings still arriving"}
    if score >= 0.38:
        return {"level": "plain", "label": "balanced market",
                "why": "steady supply; nothing forcing your hand either way"}
    return {"level": "warn", "label": "thin market",
            "why": "not much live right now; good ones will move fast when they appear"}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _age_hours(dt: datetime) -> float:
    now = datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return max(0.0, (now - dt).total_seconds() / 3600.0)


def compute_market(
    comps: list[Listing],
    context: WatchlistContext | None,
    pick_ids: list[int] | None = None,
) -> dict[str, Any]:
    """The Market analysis payload. Every field present is true at first
    runtime; band-derived fields are None/[] below MIN_COMPS."""
    prices = [c.price for c in comps]
    band = price_band(prices)
    ceiling = context.max_budget if context else None
    within = sum(1 for p in prices if p <= ceiling) if ceiling else None
    newest = min((_age_hours(c.first_seen_at) for c in comps), default=None)
    kind, rows = price_structure(comps)
    picks = set(pick_ids or [])

    return {
        "n_live": len(comps),
        "typical": round(band[1]) if band else None,
        "band": {"p25": round(band[0]), "median": round(band[1]), "p75": round(band[2])} if band else None,
        "within_budget": within,
        "ceiling": ceiling,
        "newest_find_hours": round(newest, 1) if newest is not None else None,
        "histogram": histogram(prices),
        "pick_prices": [
            {"id": c.id, "price": c.price, "over_ceiling": bool(ceiling and c.price > ceiling)}
            for c in comps if c.id in picks
        ],
        "structure": {
            "kind": kind,
            "rows": [{"label": r.label, "avg_price": round(r.avg_price), "count": r.count} for r in rows],
        },
        "heat": market_heat(len(comps), within, newest),
        "negotiation": negotiation_numbers(prices, ceiling),
    }
