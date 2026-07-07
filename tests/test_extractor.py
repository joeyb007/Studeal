"""Tests for Extractor — page-locked LLM sub-worker (v14 spec P4).

Contract exercised here:
    - Fresh LLM context per invocation (no accumulation).
    - Bounded ReAct: max 5 tool calls total; scroll capped at 3.
    - Page-locked: click/navigate are not valid tools.
    - Returns list[Offer] with valid title/price/url; malformed rows dropped.

Mock LLM: returns pre-queued JSON responses per call, records message history
so tests can assert what the extractor sent it. Uses LocalPlaywrightSession
with set_content for a real Page fixture (no network).

Tests fail on `NotImplementedError` until Day 1 impl lands.
"""

from __future__ import annotations

import pytest

from dealbot.agents.workers.extractor import Extractor, Offer
from dealbot.scrapers.browser_session import LocalPlaywrightSession
from dealbot.schemas import WatchlistContext


_SERP_HTML = """
<!doctype html>
<html>
  <head><title>Marketplace SERP</title></head>
  <body>
    <h1>Search results</h1>
    <div class="card">
      <h3>Herman Miller Aeron Size B</h3>
      <span>CA$450</span>
      <a href="https://kijiji.ca/l/aeron-1">details</a>
    </div>
    <div class="card">
      <h3>Aeron Chair Excellent Condition</h3>
      <span>CA$525</span>
      <a href="https://kijiji.ca/l/aeron-2">details</a>
    </div>
    <div class="card">
      <h3>Aeron Chair - Needs New Casters</h3>
      <span>CA$300</span>
      <a href="https://kijiji.ca/l/aeron-3">details</a>
    </div>
  </body>
</html>
"""


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def _is_playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    import os
    for p in (
        "~/Library/Caches/ms-playwright",
        "~/.cache/ms-playwright",
    ):
        if os.path.isdir(os.path.expanduser(p)):
            return True
    return False


pytestmark = pytest.mark.skipif(
    not _is_playwright_installed(),
    reason="Playwright Chromium not installed (run `playwright install chromium`).",
)


class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    """Returns pre-queued JSON responses. Records all messages sent to it."""

    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)
        self.calls: list[list[dict]] = []

    async def complete(self, messages, response_format=None, **kwargs):
        # Deep-copy so callers can mutate `messages` without corrupting history
        self.calls.append([dict(m) for m in messages])
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        # Default terminal — end the loop cleanly if test forgot to enqueue
        return _MockResponse('{"action":"emit_offers","offers":[]}')


def _spec() -> WatchlistContext:
    return WatchlistContext(
        product_query="Herman Miller Aeron",
        max_budget=700.0,
        condition=["used"],
    )


def _emit_offers_json(offers: list[dict]) -> str:
    """Build a fake emit_offers response body."""
    import json
    return json.dumps({"action": "emit_offers", "offers": offers})


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_page_returns_offers_on_emit():
    """LLM emits emit_offers with 3 valid rows → 3 Offer objects returned."""
    llm = _MockLLM([_emit_offers_json([
        {"title": "Aeron Size B", "price": 450.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-1", "marketplace": "kijiji"},
        {"title": "Aeron Excellent", "price": 525.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-2", "marketplace": "kijiji"},
        {"title": "Aeron Needs Casters", "price": 300.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/aeron-3", "marketplace": "kijiji"},
    ])])
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SERP_HTML)
        offers = await extractor.extract_page(bs.page, _spec())

    assert len(offers) == 3
    assert all(isinstance(o, Offer) for o in offers)
    assert {o.title for o in offers} == {
        "Aeron Size B", "Aeron Excellent", "Aeron Needs Casters",
    }
    assert all(o.marketplace == "kijiji" for o in offers)


@pytest.mark.asyncio
async def test_extract_page_uses_scroll_before_emit():
    """LLM emits scroll then emit_offers → both actions executed in order."""
    llm = _MockLLM([
        '{"action":"scroll"}',
        _emit_offers_json([
            {"title": "Aeron Below Fold", "price": 400.0, "currency": "CAD",
             "url": "https://kijiji.ca/l/aeron-x", "marketplace": "kijiji"},
        ]),
    ])
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SERP_HTML)
        offers = await extractor.extract_page(bs.page, _spec())

    assert len(offers) == 1
    # Two LLM turns: scroll + emit_offers
    assert len(llm.calls) == 2


@pytest.mark.asyncio
async def test_extract_page_bounded_by_max_tool_calls():
    """LLM keeps requesting scroll → loop exits at MAX_TOOL_CALLS, returns list.

    Never exceeds the tool call cap. The specific rejection mechanism (loop
    exits on cap; final call may or may not emit) is impl detail; the test
    only asserts the cap is honored and a list is returned.
    """
    llm = _MockLLM(['{"action":"scroll"}'] * 10)
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SERP_HTML)
        offers = await extractor.extract_page(bs.page, _spec())

    assert isinstance(offers, list)
    assert len(llm.calls) <= Extractor.MAX_TOOL_CALLS


@pytest.mark.asyncio
async def test_extract_page_fresh_context_across_invocations():
    """Two extract_page calls on the same Extractor → second sees no history
    from the first. Fresh LLM message list per invocation (P4 invariant)."""
    llm = _MockLLM([
        _emit_offers_json([
            {"title": "First Run", "price": 100.0, "currency": "CAD",
             "url": "https://kijiji.ca/l/first", "marketplace": "kijiji"},
        ]),
        _emit_offers_json([
            {"title": "Second Run", "price": 200.0, "currency": "CAD",
             "url": "https://kijiji.ca/l/second", "marketplace": "kijiji"},
        ]),
    ])
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SERP_HTML)
        first = await extractor.extract_page(bs.page, _spec())
        second = await extractor.extract_page(bs.page, _spec())

    assert len(first) == 1 and first[0].title == "First Run"
    assert len(second) == 1 and second[0].title == "Second Run"

    # Fresh context: second call's messages must NOT contain any msg from
    # first call. Concretely, no assistant response from the first call
    # (which mentioned "First Run") appears anywhere in the second call's
    # message history.
    first_msg_bodies = " ".join(
        str(m.get("content", "")) for m in llm.calls[0]
    )
    second_msg_bodies = " ".join(
        str(m.get("content", "")) for m in llm.calls[1]
    )
    assert "First Run" not in second_msg_bodies, (
        "second invocation must not see first invocation's assistant output"
    )
    # Sanity: first call obviously doesn't see "Second Run"
    assert "Second Run" not in first_msg_bodies


@pytest.mark.asyncio
async def test_extract_page_filters_invalid_offers():
    """Rows with price<=0 or empty url are dropped from the return value."""
    llm = _MockLLM([_emit_offers_json([
        {"title": "Valid", "price": 500.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/valid", "marketplace": "kijiji"},
        {"title": "Zero Price", "price": 0.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/zero", "marketplace": "kijiji"},
        {"title": "Missing URL", "price": 400.0, "currency": "CAD",
         "url": "", "marketplace": "kijiji"},
        {"title": "Negative", "price": -1.0, "currency": "CAD",
         "url": "https://kijiji.ca/l/neg", "marketplace": "kijiji"},
    ])])
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        await bs.page.set_content(_SERP_HTML)
        offers = await extractor.extract_page(bs.page, _spec())

    assert len(offers) == 1
    assert offers[0].title == "Valid"


@pytest.mark.asyncio
async def test_extract_page_rejects_page_locked_violations():
    """LLM emits `click` / `navigate` — extractor rejects, keeps looping.

    Page-locked contract: click and navigate are not valid tools. When the
    LLM emits one, the extractor must feed back an error and continue the
    loop (not crash, not execute the action). Eventually the LLM should
    emit_offers or the budget expires.
    """
    llm = _MockLLM([
        '{"action":"click","target":"listing-1"}',   # rejected
        '{"action":"navigate","url":"https://foo"}',  # rejected
        _emit_offers_json([]),                        # legitimate end
    ])
    extractor = Extractor(llm=llm)
    async with LocalPlaywrightSession() as bs:
        # The extractor must NOT navigate away from the fixture page.
        original_url = bs.page.url
        await bs.page.set_content(_SERP_HTML)
        offers = await extractor.extract_page(bs.page, _spec())

    assert offers == []
    assert bs.page.url == original_url or bs.page.url.startswith("about:"), (
        "extractor must not navigate the page even when LLM emits `navigate`"
    )
