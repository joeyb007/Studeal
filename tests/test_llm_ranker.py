"""Listwise reranking over a retrieved shortlist.

Retrieval is a bi-encoder and cannot resolve negation or fine distinctions;
this stage orders the shortlist and says why. It must degrade rather than fail:
a ranking outage returns candidates in retrieval order, never nothing.
"""

from __future__ import annotations

import json

import pytest

from dealbot.db.models import Listing
from dealbot.recsys.ranker import RANK_WINDOW, RankedListing, rank
from dealbot.schemas import WatchlistContext


def _listing(n: int, title: str = "") -> Listing:
    listing = Listing(
        canonical_url=f"c{n}", raw_url=f"https://m.test/{n}",
        marketplace="kijiji", title=title or f"Item {n}",
        price=100.0 + n, currency="CAD", condition="used",
    )
    listing.id = n
    return listing


class _RankingLLM:
    """Returns a fixed ranking payload; records how many calls it received."""

    def __init__(self, payload: dict) -> None:
        self._payload = payload
        self.calls = 0

    async def complete(self, messages, tools=None, response_format=None):
        self.calls += 1

        class _R:
            content = json.dumps(self._payload)

        return _R()


class _BrokenLLM:
    async def complete(self, messages, tools=None, response_format=None):
        raise RuntimeError("ranker down")


_SPEC = WatchlistContext(product_query="noise cancelling headphones", max_budget=200.0)


@pytest.mark.asyncio
async def test_orders_by_score_and_carries_reasons():
    candidates = [_listing(1), _listing(2), _listing(3)]
    llm = _RankingLLM({"rankings": [
        {"index": 2, "score": 0.9, "reason": "Over-ear ANC, well under budget."},
        {"index": 0, "score": 0.5, "reason": "Right category, no ANC."},
        {"index": 1, "score": 0.1, "reason": "Wired earbuds, not what was asked for."},
    ]})
    ranked = await rank(_SPEC, candidates, llm=llm, top_n=3)

    assert [r.listing.id for r in ranked] == [3, 1, 2]
    assert ranked[0].score == 0.9
    assert ranked[0].reason.startswith("Over-ear ANC")
    assert all(isinstance(r, RankedListing) for r in ranked)


@pytest.mark.asyncio
async def test_respects_top_n():
    candidates = [_listing(i) for i in range(1, 6)]
    llm = _RankingLLM({"rankings": [
        {"index": i, "score": 1.0 - i / 10, "reason": "r"} for i in range(5)
    ]})
    ranked = await rank(_SPEC, candidates, llm=llm, top_n=2)
    assert len(ranked) == 2


@pytest.mark.asyncio
async def test_chunks_large_candidate_sets():
    """One call per window — a single call with 60 listings ranks unreliably."""
    candidates = [_listing(i) for i in range(1, 61)]
    llm = _RankingLLM({"rankings": [{"index": 0, "score": 0.5, "reason": "r"}]})
    await rank(_SPEC, candidates, llm=llm, top_n=10)
    expected = -(-60 // RANK_WINDOW)  # ceiling division
    assert llm.calls == expected, f"expected {expected} windowed calls, got {llm.calls}"


@pytest.mark.asyncio
async def test_every_candidate_survives_chunking():
    """Windows must partition the candidate list — no listing may be dropped,
    including ones the model omitted from its ranking."""
    candidates = [_listing(i) for i in range(1, 61)]
    llm = _RankingLLM({"rankings": [
        {"index": i, "score": 0.5, "reason": "r"} for i in range(RANK_WINDOW)
    ]})
    ranked = await rank(_SPEC, candidates, llm=llm, top_n=100)
    assert len({r.listing.id for r in ranked}) == 60


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_candidate_order():
    candidates = [_listing(1), _listing(2), _listing(3)]
    ranked = await rank(_SPEC, candidates, llm=_BrokenLLM(), top_n=3)
    assert [r.listing.id for r in ranked] == [1, 2, 3], (
        "retrieval order is a real ranking; returning nothing is not"
    )
    assert all(r.score == 0.0 for r in ranked)
    assert all(r.reason == "" for r in ranked)


@pytest.mark.asyncio
async def test_malformed_json_falls_back():
    class _GarbageLLM:
        async def complete(self, messages, tools=None, response_format=None):
            class _R:
                content = "not json at all"
            return _R()

    ranked = await rank(_SPEC, [_listing(1), _listing(2)], llm=_GarbageLLM(), top_n=2)
    assert len(ranked) == 2


@pytest.mark.asyncio
async def test_out_of_range_indices_are_ignored():
    """A hallucinated index must not raise or fabricate a listing."""
    llm = _RankingLLM({"rankings": [
        {"index": 0, "score": 0.9, "reason": "real"},
        {"index": 99, "score": 0.8, "reason": "hallucinated"},
    ]})
    ranked = await rank(_SPEC, [_listing(1), _listing(2)], llm=llm, top_n=5)
    assert [r.listing.id for r in ranked] == [1, 2]


@pytest.mark.asyncio
async def test_budget_is_withheld_from_the_ranker():
    """The ceiling is a SQL filter upstream. Telling the ranker about a
    constraint it must not be able to relax is an invitation to relax it."""
    from dealbot.recsys.ranker import _spec_text

    spec_text = _spec_text(WatchlistContext(
        product_query="headphones", max_budget=200.0,
        buyer_profile="Student on a tight budget.",
    ))
    assert "200" not in spec_text
    assert "headphones" in spec_text
    assert "tight budget" in spec_text, "the profile still passes through verbatim"


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty():
    assert await rank(_SPEC, [], llm=_RankingLLM({"rankings": []})) == []


def test_spec_text_carries_must_and_nice_attributes():
    from dealbot.recsys.ranker import _spec_text
    from dealbot.schemas import SpecAttribute, WatchlistContext

    text = _spec_text(WatchlistContext(
        product_query="beginner golf clubs",
        attributes=[
            SpecAttribute(name="handedness", value="right-handed", tier="must"),
            SpecAttribute(name="flex", value="regular flex", tier="nice"),
        ],
    ))
    assert "Requirements (non-negotiable): right-handed" in text
    assert "Nice to have: regular flex" in text
    assert "NOT a contradiction" in text, "silence must never disqualify"


def test_spec_text_omits_attribute_blocks_when_empty():
    from dealbot.recsys.ranker import _spec_text
    from dealbot.schemas import WatchlistContext

    text = _spec_text(WatchlistContext(product_query="bike"))
    assert "Requirements" not in text
    assert "Nice to have" not in text
