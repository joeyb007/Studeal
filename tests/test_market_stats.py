"""Market stats: comp-set arithmetic, fallback ladder, universality rules."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from dealbot.db.models import Listing
from dealbot.recsys.market_stats import (
    MIN_COMPS,
    compute_market,
    histogram,
    market_heat,
    negotiation_numbers,
    price_read,
    price_structure,
)
from dealbot.schemas import WatchlistContext


def _listing(i: int, price: float, condition: str = "used", marketplace: str = "kijiji",
             age_hours: float = 5.0) -> Listing:
    now = datetime.now(timezone.utc)
    return Listing(
        id=i, canonical_url=f"c{i}", raw_url=f"r{i}", marketplace=marketplace,
        title=f"item {i}", price=price, currency="CAD", condition=condition,
        first_seen_at=now - timedelta(hours=age_hours),
        last_seen_at=now,
    )


PRICES = [200.0, 240.0, 260.0, 280.0, 300.0, 320.0, 360.0, 380.0]


def test_price_read_levels():
    assert price_read(280.0, PRICES)["level"] == "fair"
    assert price_read(180.0, PRICES)["level"] == "under"
    assert price_read(400.0, PRICES)["level"] == "over"
    assert price_read(280.0, PRICES[:3]) is None  # below MIN_COMPS


def test_negotiation_numbers_are_formulas():
    n = negotiation_numbers(PRICES, ceiling=300.0)
    assert n["open"] == round(0.8 * n["median"])
    assert n["fair_low"] < n["median"] <= n["fair_high"]
    assert n["walk"] <= 300.0            # never above the user's ceiling
    assert negotiation_numbers(PRICES[:2], 300.0) is None


def test_negotiation_walk_uses_p75_without_ceiling():
    n = negotiation_numbers(PRICES, ceiling=None)
    assert n["walk"] >= n["median"]


def test_histogram_counts_everything_once():
    h = histogram(PRICES)
    assert sum(b["count"] for b in h) == len(PRICES)
    assert histogram(PRICES[:3]) == []   # below MIN_COMPS


def test_structure_ladder_condition_first():
    comps = [_listing(i, 200 + 10 * i, condition="used") for i in range(4)] + \
            [_listing(10 + i, 300 + 10 * i, condition="refurbished") for i in range(4)]
    kind, rows = price_structure(comps)
    assert kind == "condition"
    assert rows[0].avg_price > rows[1].avg_price   # sorted, priciest first


def test_structure_ladder_falls_to_marketplace_then_quartiles():
    comps = [_listing(i, 200 + 10 * i, condition="unknown", marketplace="kijiji") for i in range(4)] + \
            [_listing(10 + i, 300 + 10 * i, condition="unknown", marketplace="ebay") for i in range(4)]
    kind, _ = price_structure(comps)
    assert kind == "marketplace"

    same = [_listing(i, 200 + 5 * i, condition="unknown", marketplace="kijiji") for i in range(8)]
    kind2, rows2 = price_structure(same)
    assert kind2 == "quartiles" and len(rows2) == 2

    assert price_structure(same[:3]) == ("none", [])


def test_heat_levels():
    assert market_heat(14, 12, 2.0)["level"] == "good"
    assert market_heat(2, 0, 200.0)["level"] == "warn"


def test_compute_market_first_runtime_complete():
    comps = [_listing(i, p) for i, p in enumerate(PRICES)]
    ctx = WatchlistContext(product_query="airpods max", max_budget=300.0)
    m = compute_market(comps, ctx, pick_ids=[0, 2])

    assert m["n_live"] == 8
    assert m["typical"] is not None
    assert m["within_budget"] == 5
    assert m["newest_find_hours"] is not None
    assert len(m["histogram"]) > 0
    assert len(m["pick_prices"]) == 2
    assert m["structure"]["kind"] != "none"
    assert m["heat"]["label"]
    assert m["negotiation"]["walk"] <= 300


def test_compute_market_honest_below_min_comps():
    comps = [_listing(i, 200.0 + i) for i in range(MIN_COMPS - 1)]
    m = compute_market(comps, None)
    assert m["typical"] is None
    assert m["band"] is None
    assert m["histogram"] == []
    assert m["negotiation"] is None
    assert m["n_live"] == MIN_COMPS - 1   # count itself is always honest
