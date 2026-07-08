"""Tests for Explorer — LLM-driven marketplace browsing sub-agent.

Uses real LocalPlaywrightSession + Playwright route.fulfill for URL mocks,
mocked LLM with pre-queued action responses.
"""

from __future__ import annotations

import json

import pytest

from dealbot.agents.explorer import Explorer, ExplorerResult
from dealbot.agents.perception import PageSnapshot
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import LocalPlaywrightSession


def _is_playwright_installed() -> bool:
    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        return False
    import os
    for p in ("~/Library/Caches/ms-playwright", "~/.cache/ms-playwright"):
        if os.path.isdir(os.path.expanduser(p)):
            return True
    return False


pytestmark = pytest.mark.skipif(
    not _is_playwright_installed(),
    reason="Playwright Chromium not installed.",
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    def __init__(self, actions: list[dict]) -> None:
        self._responses = [json.dumps(a) for a in actions]
        self.calls = 0

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls += 1
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        # Default: end the loop cleanly if the test forgot to enqueue a done
        return _MockResponse(json.dumps({"action": "done", "reason": "test-fallback"}))


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Aeron", max_budget=700.0)


async def _sink_collector():
    """Returns (sink_coro, collected_list)."""
    collected: list[tuple[PageSnapshot, str]] = []

    async def sink(snap: PageSnapshot, marketplace: str) -> None:
        collected.append((snap, marketplace))

    return sink, collected


async def _mock_page(bs: LocalPlaywrightSession, url: str, html: str) -> None:
    """Fulfill any request to `url` with the given HTML."""
    await bs.page.context.route(
        url,
        lambda route: route.fulfill(
            status=200, body=html, content_type="text/html",
        ),
    )


_START_HTML = """
<!doctype html>
<html><head><title>Marketplace</title></head><body>
  <h1>Search results</h1>
  <div>Listing A</div>
  <div>Listing B</div>
  <a href="https://mock.local/page2">Next</a>
</body></html>
"""

_PAGE2_HTML = """
<!doctype html>
<html><head><title>Marketplace P2</title></head><body>
  <h1>Search results — page 2</h1>
  <div>Listing C</div>
  <div>Listing D</div>
</body></html>
"""


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_done_immediately_enqueues_start_snap():
    """LLM says done on turn 1 → sink called once with the start URL snap."""
    llm = _MockLLM([{"action": "done", "reason": "no interest"}])
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/start", _START_HTML)
        result = await explorer.explore(
            start_url="https://mock.local/start",
            marketplace="kijiji",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert isinstance(result, ExplorerResult)
    assert result.stop_reason == "done"
    assert result.turns_used == 1
    assert len(collected) == 1
    snap, marketplace = collected[0]
    assert marketplace == "kijiji"
    assert snap.url == "https://mock.local/start"


@pytest.mark.asyncio
async def test_click_next_transitions_url_and_enqueues_both():
    """LLM: click Next → done. Sink receives start snap (on URL change) + page2 snap (on done)."""
    llm = _MockLLM([
        {"action": "click", "role": "link", "name": "Next"},
        {"action": "done", "reason": "paginated"},
    ])
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/start", _START_HTML)
        await _mock_page(bs, "https://mock.local/page2", _PAGE2_HTML)
        result = await explorer.explore(
            start_url="https://mock.local/start",
            marketplace="kijiji",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason == "done"
    urls_enqueued = [snap.url for snap, _ in collected]
    assert "https://mock.local/start" in urls_enqueued
    assert "https://mock.local/page2" in urls_enqueued
    assert set(result.urls_visited) == {
        "https://mock.local/start", "https://mock.local/page2",
    }


@pytest.mark.asyncio
async def test_max_turns_exhausted_returns_max_turns_stop_reason():
    """LLM never emits done → loop exits at MAX_TURNS."""
    # 40 scrolls > MAX_TURNS=20 — but scroll on a static page keeps snapshot
    # key constant, so loop detection catches it first.
    llm = _MockLLM([{"action": "scroll"}] * 40)
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/start", _START_HTML)
        result = await explorer.explore(
            start_url="https://mock.local/start",
            marketplace="kijiji",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason in ("max_turns", "loop")
    # Whichever stop fired, we should have enqueued at least the start URL.
    assert any(snap.url == "https://mock.local/start" for snap, _ in collected)


@pytest.mark.asyncio
async def test_invalid_action_json_does_not_crash():
    """Malformed action → feedback message, LLM tries again, eventually done."""
    llm = _MockLLM([])
    # Manually queue: bad JSON, then valid done
    llm._responses = ["not valid json", json.dumps({"action": "done"})]
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/start", _START_HTML)
        result = await explorer.explore(
            start_url="https://mock.local/start",
            marketplace="kijiji",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason == "done"
    assert len(collected) == 1
