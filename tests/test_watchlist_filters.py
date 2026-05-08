from __future__ import annotations

import pytest

from dealbot.schemas import WatchlistContext


def _make_deal(
    id: int,
    sale_price: float,
    listed_price: float,
    real_discount_pct: float | None,
    condition: str,
    title: str = "Test Deal",
    source: str = "Amazon",
    score: int = 75,
):
    from dealbot.db.models import Deal
    d = Deal()
    d.id = id
    d.title = title
    d.source = source
    d.url = f"https://example.com/{id}"
    d.affiliate_url = None
    d.sale_price = sale_price
    d.listed_price = listed_price
    d.real_discount_pct = real_discount_pct
    d.condition = condition
    d.score = score
    d.alert_tier = "digest"
    d.category = "Electronics"
    d.student_eligible = False
    return d


def _apply_filters(deals, ctx: WatchlistContext):
    """Mirror the filter logic from the route for unit testing."""
    filtered = list(deals)

    if ctx.max_budget:
        filtered = [d for d in filtered if d.sale_price <= ctx.max_budget]
    if ctx.condition:
        filtered = [d for d in filtered if d.condition in ctx.condition]
    if ctx.brands:
        filtered = [
            d for d in filtered
            if any(b.lower() in d.title.lower() or b.lower() in d.source.lower() for b in ctx.brands)
        ]

    if ctx.min_discount_pct:
        strict = [
            d for d in filtered
            if d.real_discount_pct and d.real_discount_pct >= ctx.min_discount_pct
        ]
        if strict:
            return strict, True
        return filtered, False

    return filtered, True


def test_filter_by_max_budget():
    deals = [
        _make_deal(1, sale_price=500.0, listed_price=800.0, real_discount_pct=37.5, condition="new"),
        _make_deal(2, sale_price=1200.0, listed_price=1500.0, real_discount_pct=20.0, condition="new"),
    ]
    ctx = WatchlistContext(product_query="laptop", keywords=[], max_budget=800.0)
    result, filtered = _apply_filters(deals, ctx)
    assert len(result) == 1
    assert result[0].id == 1
    assert filtered is True


def test_filter_by_condition():
    deals = [
        _make_deal(1, 200, 300, 33, "new"),
        _make_deal(2, 150, 300, 50, "used"),
    ]
    ctx = WatchlistContext(product_query="headphones", keywords=[], condition=["new"])
    result, _ = _apply_filters(deals, ctx)
    assert len(result) == 1
    assert result[0].id == 1


def test_min_discount_fallback_when_no_matches():
    deals = [
        _make_deal(1, 200, 220, 9.0, "new"),
        _make_deal(2, 180, 200, 10.0, "new"),
    ]
    ctx = WatchlistContext(product_query="headphones", keywords=[], min_discount_pct=30)
    result, filtered = _apply_filters(deals, ctx)
    assert len(result) == 2
    assert filtered is False


def test_min_discount_strict_when_matches_exist():
    deals = [
        _make_deal(1, 200, 300, 33.0, "new"),
        _make_deal(2, 180, 200, 10.0, "new"),
    ]
    ctx = WatchlistContext(product_query="headphones", keywords=[], min_discount_pct=30)
    result, filtered = _apply_filters(deals, ctx)
    assert len(result) == 1
    assert result[0].id == 1
    assert filtered is True


def test_filter_by_brand():
    deals = [
        _make_deal(1, 200, 300, 33.0, "new", title="Sony WH-1000XM5"),
        _make_deal(2, 150, 200, 25.0, "new", title="Bose QuietComfort 45"),
    ]
    ctx = WatchlistContext(product_query="headphones", keywords=[], brands=["Sony"])
    result, _ = _apply_filters(deals, ctx)
    assert len(result) == 1
    assert result[0].id == 1


def test_no_context_returns_all_deals():
    deals = [
        _make_deal(1, 200, 300, 33.0, "new"),
        _make_deal(2, 500, 600, 16.0, "refurb"),
    ]
    ctx = WatchlistContext(product_query="laptop", keywords=[])
    result, filtered = _apply_filters(deals, ctx)
    assert len(result) == 2
    assert filtered is True
