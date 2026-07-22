"""Tests for NaiveReActRunner — ablation baseline.

Uses real LocalPlaywrightSession + Playwright route.fulfill for URL mocks,
mocked LLM with pre-queued action responses. Same conventions as test_explorer.py.
"""

from __future__ import annotations

import json
import os

import pytest

from dealbot.agents.marketplace_router import MarketplaceSearchTarget
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import LocalPlaywrightSession
from tests.evals.naive_react import NaiveReActRunner


# ---------------------------------------------------------------------------
# Playwright gate (same pattern as test_explorer.py)
# ---------------------------------------------------------------------------

def _is_playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    for p in ("~/Library/Caches/ms-playwright", "~/.cache/ms-playwright"):
        if os.path.isdir(os.path.expanduser(p)):
            return True
    return False


pytestmark = pytest.mark.skipif(
    not _is_playwright_installed(),
    reason="Playwright Chromium not installed.",
)


# ---------------------------------------------------------------------------
# Mock LLM and helpers
# ---------------------------------------------------------------------------

class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    """Pre-queued responses; falls back to done on exhaustion."""

    def __init__(self, actions: list[dict]) -> None:
        self._responses = [json.dumps(a) for a in actions]
        self.calls = 0
        self.supports_vision = False
        # Track message counts per call for context-accumulation test
        self.message_counts: list[int] = []

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls += 1
        self.message_counts.append(len(messages))
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse(json.dumps({"action": "done", "reason": "test-fallback"}))


def _spec() -> WatchlistContext:
    return WatchlistContext(
        product_query="Herman Miller Aeron chair Toronto",
        max_budget=700.0,
        brands=["Herman Miller"],
    )


def _targets(urls: list[str] | None = None) -> list[MarketplaceSearchTarget]:
    if urls is None:
        urls = ["https://mock.local/search"]
    return [
        MarketplaceSearchTarget(marketplace=f"site{i}", entry_url=url)
        for i, url in enumerate(urls)
    ]


async def _mock_page(session: LocalPlaywrightSession, url: str, html: str) -> None:
    await session.page.context.route(
        url,
        lambda route: route.fulfill(
            status=200, body=html, content_type="text/html",
        ),
    )


_SEARCH_HTML = """<!doctype html>
<html><head><title>Search Results</title></head><body>
  <h1>Results</h1>
  <div>
    <a href="https://mock.local/listing/1">Aeron Chair</a>
    <span>$450</span>
  </div>
  <div>
    <a href="https://mock.local/listing/2">Herman Miller Aeron</a>
    <span>$500</span>
  </div>
</body></html>"""

_SITE2_HTML = """<!doctype html>
<html><head><title>Site 2 Results</title></head><body>
  <h1>Site 2</h1>
  <p>No listings here</p>
</body></html>"""


# ---------------------------------------------------------------------------
# Test 1: record_offer actions accumulate offers with absolute URLs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_record_offer_accumulates_offers():
    """record_offer actions build up the offers list; relative URLs are resolved."""
    llm = _MockLLM([
        {
            "action": "record_offer",
            "title": "Herman Miller Aeron Chair",
            "price": 450.0,
            "url": "/listing/1",   # relative — must be resolved
            "condition": "used",
        },
        {
            "action": "record_offer",
            "title": "Herman Miller Aeron Used",
            "price": 500.0,
            "url": "https://mock.local/listing/2",  # already absolute
            "condition": "used",
        },
        {"action": "done", "reason": "all offers recorded"},
    ])
    runner = NaiveReActRunner(llm)

    async with LocalPlaywrightSession() as session:
        await _mock_page(session, "https://mock.local/search", _SEARCH_HTML)
        result = await runner.run(
            spec=_spec(),
            targets=_targets(["https://mock.local/search"]),
            session=session,
        )

    assert result.stop_reason == "done"
    assert len(result.offers) == 2

    urls = [o.url for o in result.offers]
    # Both must be absolute
    assert all(u.startswith("http") for u in urls), f"non-absolute URL in {urls}"
    # Relative /listing/1 must be resolved to absolute
    assert any("listing/1" in u for u in urls), f"relative URL not resolved: {urls}"
    # Currency must default to CAD
    assert all(o.currency == "CAD" for o in result.offers)
    # Condition preserved
    assert all(o.condition == "used" for o in result.offers)


# ---------------------------------------------------------------------------
# Test 2: next_site advances, then done ends with stop_reason "done"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_next_site_then_done():
    """next_site advances to the second target; done ends with stop_reason 'done'."""
    llm = _MockLLM([
        {
            "action": "record_offer",
            "title": "Aeron Chair on site 0",
            "price": 300.0,
            "url": "https://mock.local/listing/10",
            "condition": "used",
        },
        {"action": "next_site"},
        {"action": "done", "reason": "all sites visited"},
    ])
    runner = NaiveReActRunner(llm)

    async with LocalPlaywrightSession() as session:
        await _mock_page(session, "https://mock.local/site1", _SEARCH_HTML)
        await _mock_page(session, "https://mock.local/site2", _SITE2_HTML)
        result = await runner.run(
            spec=_spec(),
            targets=_targets(["https://mock.local/site1", "https://mock.local/site2"]),
            session=session,
        )

    assert result.stop_reason == "done"
    # One offer from site 0 before next_site
    assert len(result.offers) == 1
    assert result.offers[0].title == "Aeron Chair on site 0"
    # Both sites were attempted (3 LLM calls: record_offer + next_site + done)
    assert llm.calls == 3


# ---------------------------------------------------------------------------
# Test 3: context accumulates monotonically (distinguishing property)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_context_accumulates_monotonically():
    """The message list grows strictly larger each turn — the defining property
    that distinguishes NaiveReActRunner from Explorer (which trims snapshots)."""
    # Enough turns to observe the accumulation across >= 4 calls
    llm = _MockLLM([
        {
            "action": "record_offer",
            "title": "Offer A",
            "price": 100.0,
            "url": "https://mock.local/a",
            "condition": "used",
        },
        {
            "action": "record_offer",
            "title": "Offer B",
            "price": 200.0,
            "url": "https://mock.local/b",
            "condition": "new",
        },
        {
            "action": "record_offer",
            "title": "Offer C",
            "price": 300.0,
            "url": "https://mock.local/c",
            "condition": "unknown",
        },
        {"action": "done", "reason": "done"},
    ])
    runner = NaiveReActRunner(llm)

    async with LocalPlaywrightSession() as session:
        await _mock_page(session, "https://mock.local/search", _SEARCH_HTML)
        result = await runner.run(
            spec=_spec(),
            targets=_targets(["https://mock.local/search"]),
            session=session,
        )

    assert result.stop_reason == "done"
    counts = llm.message_counts
    # NOTE: validates accumulation in the trim-free regime — mock HTML is tiny so
    # the 100k-char guard never fires here; on real-page runs context will be
    # trimmed after the guard, so counts may dip — that is by design, not a bug.
    # At least 4 calls observed
    assert len(counts) >= 4, f"expected >= 4 LLM calls, got {len(counts)}"
    # Message count strictly increases across all calls (context accumulates)
    for i in range(1, len(counts)):
        assert counts[i] > counts[i - 1], (
            f"message count did not increase: call {i - 1}={counts[i - 1]}, "
            f"call {i}={counts[i]}. Context is not accumulating."
        )


# ---------------------------------------------------------------------------
# Test 4: invalid record_offer (missing price) does not crash
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_invalid_record_offer_missing_price_does_not_crash():
    """A record_offer missing price must not raise; offer not added to the list."""
    llm = _MockLLM([
        {
            "action": "record_offer",
            "title": "Aeron Chair",
            # price intentionally omitted
            "url": "https://mock.local/listing/99",
            "condition": "used",
        },
        # After the error tool-result, the agent calls done
        {"action": "done", "reason": "finished"},
    ])
    runner = NaiveReActRunner(llm)

    async with LocalPlaywrightSession() as session:
        await _mock_page(session, "https://mock.local/search", _SEARCH_HTML)
        result = await runner.run(
            spec=_spec(),
            targets=_targets(["https://mock.local/search"]),
            session=session,
        )

    assert result.stop_reason == "done"
    # Offer must NOT have been added
    assert len(result.offers) == 0, (
        f"invalid offer (missing price) must not be recorded, got {result.offers}"
    )
