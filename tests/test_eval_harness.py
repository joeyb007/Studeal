"""Offline tests for the eval harness core.

Covers:
    (a) _resolve_targets: template mode returns correct URL from CURATED_MARKETPLACES;
        unknown name raises ValueError.
    (b) _resolve_targets: home mode returns site home URL, not search URL.
    (c) _passed_bar: all four clauses each independently violate the bar.
    (d) append_result: writes valid JSONL line + creates / appends markdown table.

All tests are offline — no network, no LLM, no browser.
"""

from __future__ import annotations

import json
import os

import pytest

from tests.evals.harness import (
    CaseResult,
    _passed_bar,
    _resolve_targets,
    append_result,
    offer_matches_spec,
)
from dealbot.agents.workers.extractor import Offer
from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_result(**overrides) -> CaseResult:
    """Return a CaseResult that just clears the bar, with optional overrides."""
    defaults = dict(
        case_id="test-001",
        spec_query="aeron chair",
        marketplaces=["kijiji", "ebay"],
        entry_mode="template",
        offers_total=12,
        offers_unique=10,
        offers_by_marketplace={"kijiji": 6, "ebay": 6},
        marketplaces_with_listings=2,
        wall_clock_s=100.0,
        stop_reasons={},
        error_stops=0,
        nav_prompt_tokens=1000,
        nav_completion_tokens=200,
        extract_prompt_tokens=3000,
        extract_completion_tokens=400,
        est_cost_usd=0.05,
        passed_bar=True,
        precision=None,
    )
    defaults.update(overrides)
    return CaseResult(**defaults)


def _make_spec(
    product_query: str = "Herman Miller Aeron chair",
    brands: list[str] | None = None,
    max_budget: float | None = 700.0,
) -> WatchlistContext:
    return WatchlistContext(
        product_query=product_query,
        max_budget=max_budget,
        condition=["used", "like new"],
        brands=brands if brands is not None else ["Herman Miller"],
        keywords=["aeron"],
    )


def _make_offer(
    title: str = "Herman Miller Aeron Chair - used",
    price: float = 500.0,
    url: str = "https://kijiji.ca/listing/1",
    marketplace: str = "kijiji",
) -> Offer:
    return Offer(title=title, price=price, url=url, marketplace=marketplace)


# ---------------------------------------------------------------------------
# (a) _resolve_targets — template mode
# ---------------------------------------------------------------------------

class TestResolveTargetsTemplate:
    def test_known_marketplace_returns_entry(self):
        targets = _resolve_targets(["kijiji"], "aeron chair", "template")
        assert len(targets) == 1
        t = targets[0]
        assert t.marketplace == "kijiji"
        # build_search_url("aeron chair") for kijiji uses hyphens and /b-buy-sell/
        assert "kijiji.ca" in t.entry_url
        assert "aeron" in t.entry_url.lower() or "aeron-chair" in t.entry_url.lower()

    def test_multiple_known_marketplaces(self):
        targets = _resolve_targets(["kijiji", "ebay"], "macbook", "template")
        assert len(targets) == 2
        keys = {t.marketplace for t in targets}
        assert keys == {"kijiji", "ebay"}

    def test_unknown_marketplace_raises_value_error(self):
        with pytest.raises(ValueError, match="unknown marketplace"):
            _resolve_targets(["does_not_exist"], "laptop", "template")

    def test_template_url_contains_query_fragment(self):
        targets = _resolve_targets(["ebay"], "macbook pro", "template")
        assert len(targets) == 1
        # eBay uses quote_plus; "macbook pro" → "macbook+pro"
        assert "macbook" in targets[0].entry_url.lower()


# ---------------------------------------------------------------------------
# (b) _resolve_targets — home mode
# ---------------------------------------------------------------------------

class TestResolveTargetsHome:
    def test_home_mode_returns_root_url(self):
        targets = _resolve_targets(["kijiji"], "aeron chair", "home")
        assert len(targets) == 1
        t = targets[0]
        # Should be the site root, not a search SERP
        assert "kijiji.ca" in t.entry_url
        # Should NOT contain the query string
        assert "aeron" not in t.entry_url.lower()

    def test_home_mode_url_does_not_contain_search_params(self):
        targets = _resolve_targets(["ebay"], "macbook", "home")
        t = targets[0]
        assert "macbook" not in t.entry_url.lower()
        assert "?" not in t.entry_url  # no query string in home URL

    def test_home_mode_differs_from_template_mode(self):
        home_targets = _resolve_targets(["kijiji"], "herman miller", "home")
        template_targets = _resolve_targets(["kijiji"], "herman miller", "template")
        assert home_targets[0].entry_url != template_targets[0].entry_url

    def test_fb_marketplace_home_mode_uses_override(self):
        targets = _resolve_targets(["fb_marketplace"], "chair", "home")
        assert len(targets) == 1
        t = targets[0]
        assert t.entry_url == "https://www.facebook.com/marketplace/"

    def test_kijiji_home_mode_still_works(self):
        targets = _resolve_targets(["kijiji"], "chair", "home")
        assert len(targets) == 1
        t = targets[0]
        assert t.entry_url.startswith("https://www.kijiji.ca")


# ---------------------------------------------------------------------------
# (c) _passed_bar — all four clauses
# ---------------------------------------------------------------------------

class TestPassedBar:
    def test_passes_with_all_conditions_met(self):
        r = _make_result(
            offers_total=12,
            marketplaces_with_listings=2,
            wall_clock_s=100.0,
            error_stops=0,
        )
        assert _passed_bar(r) is True

    def test_fails_when_offers_total_below_10(self):
        r = _make_result(offers_total=9)
        assert _passed_bar(r) is False

    def test_passes_at_exactly_10_offers(self):
        r = _make_result(offers_total=10)
        assert _passed_bar(r) is True

    def test_fails_when_marketplaces_with_listings_below_2(self):
        r = _make_result(marketplaces_with_listings=1)
        assert _passed_bar(r) is False

    def test_passes_at_exactly_2_marketplaces_with_listings(self):
        r = _make_result(marketplaces_with_listings=2)
        assert _passed_bar(r) is True

    def test_fails_when_wall_clock_at_or_above_240s(self):
        r = _make_result(wall_clock_s=240.0)
        assert _passed_bar(r) is False

    def test_fails_when_wall_clock_exceeds_240s(self):
        r = _make_result(wall_clock_s=300.0)
        assert _passed_bar(r) is False

    def test_passes_just_under_240s(self):
        r = _make_result(wall_clock_s=239.9)
        assert _passed_bar(r) is True

    def test_fails_when_error_stops_nonzero(self):
        r = _make_result(error_stops=1)
        assert _passed_bar(r) is False

    def test_passes_with_zero_error_stops(self):
        r = _make_result(error_stops=0)
        assert _passed_bar(r) is True


# ---------------------------------------------------------------------------
# (d) append_result — JSONL + markdown
# ---------------------------------------------------------------------------

class TestAppendResult:
    def test_creates_jsonl_file_and_writes_valid_line(self, tmp_path):
        out_dir = str(tmp_path / "evals")
        r = _make_result()
        append_result(r, out_dir=out_dir)

        jsonl_path = tmp_path / "evals" / "results.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["case_id"] == "test-001"
        assert obj["offers_total"] == 12

    def test_creates_markdown_file_with_header_on_first_write(self, tmp_path):
        out_dir = str(tmp_path / "evals")
        r = _make_result()
        append_result(r, out_dir=out_dir)

        md_path = tmp_path / "evals" / "results.md"
        assert md_path.exists()
        content = md_path.read_text()
        # Header row present
        assert "case_id" in content
        assert "|" in content

    def test_appends_second_result_without_duplicate_header(self, tmp_path):
        out_dir = str(tmp_path / "evals")
        r1 = _make_result(case_id="test-001")
        r2 = _make_result(case_id="test-002")
        append_result(r1, out_dir=out_dir)
        append_result(r2, out_dir=out_dir)

        jsonl_path = tmp_path / "evals" / "results.jsonl"
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 2

        md_path = tmp_path / "evals" / "results.md"
        content = md_path.read_text()
        # Header should appear only once
        assert content.count("case_id") == 1
        # Both case IDs present
        assert "test-001" in content
        assert "test-002" in content

    def test_jsonl_line_contains_all_required_fields(self, tmp_path):
        out_dir = str(tmp_path / "evals")
        r = _make_result()
        append_result(r, out_dir=out_dir)

        jsonl_path = tmp_path / "evals" / "results.jsonl"
        obj = json.loads(jsonl_path.read_text().strip())
        required = {
            "case_id", "spec_query", "marketplaces", "entry_mode",
            "offers_total", "offers_unique", "offers_by_marketplace",
            "marketplaces_with_listings", "wall_clock_s", "stop_reasons",
            "error_stops", "nav_prompt_tokens", "nav_completion_tokens",
            "extract_prompt_tokens", "extract_completion_tokens",
            "est_cost_usd", "passed_bar",
        }
        assert required.issubset(obj.keys())
        assert "precision" in obj


# ---------------------------------------------------------------------------
# offer_matches_spec unit tests
# ---------------------------------------------------------------------------

class TestOfferMatchesSpec:
    """Offline unit tests for the precision heuristic."""

    def test_clean_match(self):
        """Brand token present, query token present, price within budget → True."""
        offer = _make_offer(
            title="Herman Miller Aeron Chair used",
            price=500.0,
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron chair Toronto",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is True

    def test_brand_token_missing_returns_false(self):
        """Title lacks any brand token → False."""
        offer = _make_offer(
            title="Aeron Chair used",  # no "Herman" or "Miller"
            price=500.0,
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron chair",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is False

    def test_price_30_percent_over_budget_returns_false(self):
        """Price at 130% of budget (> 120% threshold) → False."""
        offer = _make_offer(
            title="Herman Miller Aeron Chair used",
            price=910.0,  # 700 * 1.3 = 910 > 700 * 1.2 = 840
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron chair",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is False

    def test_no_budget_set_with_brand_and_query_match_returns_true(self):
        """No max_budget → price clause skipped; brand + query match → True."""
        offer = _make_offer(
            title="Herman Miller Aeron Chair used",
            price=9999.0,  # would fail if budget were checked
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron chair",
            brands=["Herman Miller"],
            max_budget=None,
        )
        assert offer_matches_spec(offer, spec) is True

    def test_title_matching_only_stopwords_returns_false(self):
        """Query tokens that survive stopword + brand filtering are absent → False."""
        offer = _make_offer(
            title="Herman Miller for sale",  # brand present; only stopwords remain
            price=500.0,
        )
        # product_query stripped of brand tokens ("herman", "miller") and
        # stopwords ("for", "in", "of") → remaining tokens: [] → no match
        spec = _make_spec(
            product_query="Herman Miller for in of",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is False

    def test_case_result_zero_offers_precision_is_none(self):
        """CaseResult with offers_total=0 should carry precision=None."""
        r = _make_result(offers_total=0, precision=None)
        assert r.precision is None

    def test_price_zero_within_budget_returns_true(self):
        """Price of 0.0 is within any budget (0 <= 840) → True."""
        offer = _make_offer(
            title="Herman Miller Aeron Chair used",
            price=0.0,
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron chair",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is True

    def test_price_zero_but_no_query_token_match_returns_false(self):
        """Price 0.0 with no query/brand token match → False (price isn't only gate)."""
        offer = _make_offer(
            title="Herman Miller for sale",  # brand present; only stopwords remain
            price=0.0,
        )
        spec = _make_spec(
            product_query="Herman Miller Aeron",
            brands=["Herman Miller"],
            max_budget=700.0,
        )
        assert offer_matches_spec(offer, spec) is False


def test_estimate_cost_is_model_aware():
    """nav cost must follow the actual nav model, not a fixed gpt-4o rate.
    Regression: ablation gpt-4o-mini nav cells were charged ~17x over."""
    from tests.evals.harness import _estimate_cost

    # 1M nav prompt tokens at gpt-4o = $2.50; at gpt-4o-mini = $0.15
    c_4o = _estimate_cost(1_000_000, 0, 0, 0, nav_model="gpt-4o")
    c_mini = _estimate_cost(1_000_000, 0, 0, 0, nav_model="gpt-4o-mini")
    assert abs(c_4o - 2.50) < 1e-6
    assert abs(c_mini - 0.15) < 1e-6
    # unknown model → conservative gpt-4o rate
    assert abs(_estimate_cost(1_000_000, 0, 0, 0, nav_model="mystery") - 2.50) < 1e-6
