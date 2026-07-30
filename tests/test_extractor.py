"""Tests for Extractor — snapshot-to-Offers worker.

Contract exercised:
    - Single LLM call per invocation, no browser interaction.
    - Fresh message list per call (no accumulation).
    - `marketplace` stamped from caller; LLM's own value ignored.
    - Malformed offers (empty url, non-positive price) dropped.
    - LLM failure / malformed JSON → returns [], never raises.
"""

from __future__ import annotations

import json

import pytest

from dealbot.agents.perception import PageSnapshot
from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse('{"offers":[]}')


class _RaisingLLM:
    async def complete(self, messages, response_format=None, **kwargs):
        raise RuntimeError("simulated LLM outage")


def _snap(url: str = "https://kijiji.ca/b-buy?q=aeron", text: str = "listings...") -> PageSnapshot:
    return PageSnapshot(
        text=text,
        element_map={},
        url=url,
        title="SERP",
        char_count=len(text),
    )


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Herman Miller Aeron", max_budget=700.0)


def _offers_json(offers: list[dict]) -> str:
    return json.dumps({"offers": offers})


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_returns_parsed_offers():
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Size B", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-1"},
        {"title": "Aeron Excellent", "price": 525.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-2"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 2
    assert all(isinstance(o, Offer) for o in offers)
    assert {o.title for o in offers} == {"Aeron Size B", "Aeron Excellent"}
    assert all(o.marketplace == "kijiji" for o in offers)


@pytest.mark.asyncio
async def test_marketplace_stamped_from_caller_not_llm():
    """LLM emits a bogus marketplace; caller's value wins."""
    llm = _MockLLM([_offers_json([
        {"title": "X", "price": 100.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/x", "marketplace": "hallucinated"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].marketplace == "kijiji"


@pytest.mark.asyncio
async def test_filters_invalid_offers():
    llm = _MockLLM([_offers_json([
        {"title": "Valid", "price": 500.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/valid"},
        {"title": "Zero", "price": 0.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/zero"},
        {"title": "Empty URL", "price": 400.0, "currency": "CAD", "url": ""},
        {"title": "Negative", "price": -1.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/neg"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].title == "Valid"


@pytest.mark.asyncio
async def test_llm_failure_returns_empty_no_raise():
    extractor = Extractor(llm=_RaisingLLM())
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    assert offers == []


@pytest.mark.asyncio
async def test_malformed_json_returns_empty():
    llm = _MockLLM(["not valid json"])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    assert offers == []


@pytest.mark.asyncio
async def test_fresh_context_across_invocations():
    """Two extract calls → second call's messages contain no content from first."""
    llm = _MockLLM([
        _offers_json([{"title": "First Run", "price": 100.0, "currency": "CAD",
                       "url": "https://kijiji.ca/l/first"}]),
        _offers_json([{"title": "Second Run", "price": 200.0, "currency": "CAD",
                       "url": "https://kijiji.ca/l/second"}]),
    ])
    extractor = Extractor(llm=llm)
    first = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())
    second = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert first[0].title == "First Run"
    assert second[0].title == "Second Run"

    first_msg_bodies = " ".join(str(m.get("content", "")) for m in llm.calls[0])
    second_msg_bodies = " ".join(str(m.get("content", "")) for m in llm.calls[1])
    assert "First Run" not in second_msg_bodies
    assert "Second Run" not in first_msg_bodies


@pytest.mark.asyncio
async def test_relative_offer_urls_resolved_against_page():
    """Relative FB Marketplace URLs are resolved against the snapshot page URL."""
    fb_snap = _snap(url="https://www.facebook.com/marketplace/toronto/search?query=aeron")
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Chair", "price": 400.0, "currency": "CAD",
         "url": "/marketplace/item/123/?ref=search"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(fb_snap, "facebook", _spec())

    assert len(offers) == 1
    assert offers[0].url == "https://www.facebook.com/marketplace/item/123/?ref=search"


@pytest.mark.asyncio
async def test_absolute_offer_urls_pass_through_unchanged():
    """Offers with already-absolute URLs are not modified by urljoin."""
    llm = _MockLLM([_offers_json([
        {"title": "Aeron Size B", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-specific-listing"},
    ])])
    extractor = Extractor(llm=llm)
    offers = await extractor.extract_from_snapshot(_snap(), "kijiji", _spec())

    assert len(offers) == 1
    assert offers[0].url == "https://kijiji.ca/l/aeron-specific-listing"


# ---------------------------------------------------------------------
# A4 hardening: tolerant row normalization + href clipping
# ---------------------------------------------------------------------

from dealbot.agents.workers.extractor import _clip_hrefs, _parse_and_filter as _paf


def _row_json(**over):
    import json as _json
    row = {"title": "Sony WH-1000XM5", "price": 153.99, "currency": "CAD",
           "url": "https://www.ebay.ca/itm/1"}
    row.update(over)
    return _json.dumps({"offers": [row]})


def test_price_string_with_currency_prefix_is_coerced():
    offers = _paf(_row_json(price="C $153.99", currency="CAD"), "ebay", "https://www.ebay.ca/s")
    assert len(offers) == 1
    assert offers[0].price == 153.99
    assert offers[0].currency == "CAD"


def test_price_string_infers_currency_when_missing():
    offers = _paf(_row_json(price="C $1,234.50", currency="USD"), "ebay", "https://www.ebay.ca/s")
    assert len(offers) == 1
    assert offers[0].price == 1234.50


def test_condition_synonyms_normalize():
    for raw, want in [("Pre-Owned", "used"), ("open box", "used"),
                      ("Certified Refurbished", "refurbished"), ("Brand New", "new"),
                      ("parts only", "unknown")]:
        offers = _paf(_row_json(condition=raw), "ebay", "https://www.ebay.ca/s")
        assert len(offers) == 1, raw
        assert offers[0].condition == want, raw


def test_unparseable_price_still_dropped():
    assert _paf(_row_json(price="contact seller"), "ebay", "https://x.ca") == []


def test_clip_hrefs_shortens_only_long_urls():
    text = '[1]<a href="https://e.ca/itm/1?' + "x" * 500 + '" /> "A" [2]<a href="https://e.ca/b" /> "B"'
    out = _clip_hrefs(text)
    assert len(out) < len(text)
    assert 'href="https://e.ca/b"' in out
    assert "…" in out
