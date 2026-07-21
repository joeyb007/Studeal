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
        return _MockResponse(json.dumps({"action": "done", "reason": "test-fallback"}))


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Aeron", max_budget=700.0)


async def _sink_collector():
    collected: list[tuple[PageSnapshot, str]] = []

    async def sink(snap: PageSnapshot, marketplace: str) -> None:
        collected.append((snap, marketplace))

    return sink, collected


async def _mock_page(bs: LocalPlaywrightSession, url: str, html: str) -> None:
    await bs.page.context.route(
        url,
        lambda route: route.fulfill(
            status=200, body=html, content_type="text/html",
        ),
    )


# Home page with a labelled search form. Form submits GET to /search which
# maps to the SERP page below.
_HOME_HTML = """
<!doctype html>
<html><head><title>Marketplace Home</title></head><body>
  <h1>Welcome</h1>
  <form action="https://mock.local/search" method="get">
    <label for="q">Search</label>
    <input id="q" name="q" type="text" aria-label="Search">
  </form>
</body></html>
"""

_SERP_HTML = """
<!doctype html>
<html><head><title>Marketplace SERP</title></head><body>
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
</body></html>
"""


# ---------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_done_immediately_enqueues_entry_snap():
    """LLM says done on turn 1 → sink called once with the entry URL snap."""
    llm = _MockLLM([{"action": "done", "reason": "no_results on this page"}])
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/home", _HOME_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/home",
            marketplace="kijiji",
            query="aeron",
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
    assert snap.url == "https://mock.local/home"


@pytest.mark.asyncio
async def test_type_submits_form_and_navigates_to_serp():
    """LLM types 'aeron' into search bar; form submits; URL becomes /search?q=..."""
    llm = _MockLLM([
        {"action": "type", "role": "textbox", "name": "Search", "text": "aeron"},
        {"action": "done", "reason": "search executed"},
    ])
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/home", _HOME_HTML)
        # Any /search?* URL returns the SERP HTML.
        await bs.page.context.route(
            "https://mock.local/search*",
            lambda route: route.fulfill(
                status=200, body=_SERP_HTML, content_type="text/html",
            ),
        )
        result = await explorer.explore(
            entry_url="https://mock.local/home",
            marketplace="kijiji",
            query="aeron",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason == "done"
    urls = [snap.url for snap, _ in collected]
    # Both the home and the SERP should have been enqueued.
    assert any(u.startswith("https://mock.local/home") for u in urls)
    assert any("mock.local/search" in u for u in urls)


@pytest.mark.asyncio
async def test_click_next_transitions_url_and_enqueues_both():
    """LLM: click Next → done. Sink receives entry snap + destination snap."""
    llm = _MockLLM([
        {"action": "click", "role": "link", "name": "Next"},
        {"action": "done", "reason": "paginated"},
    ])
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/serp", _SERP_HTML)
        await _mock_page(bs, "https://mock.local/page2", _PAGE2_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/serp",
            marketplace="kijiji",
            query="aeron",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason == "done"
    urls = [snap.url for snap, _ in collected]
    assert "https://mock.local/serp" in urls
    assert "https://mock.local/page2" in urls


@pytest.mark.asyncio
async def test_max_turns_or_stall_when_llm_never_done():
    """LLM only scrolls → stall detection or max_turns stops the run."""
    llm = _MockLLM([{"action": "scroll"}] * 40)
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/home", _HOME_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/home",
            marketplace="kijiji",
            query="aeron",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason in ("max_turns", "stalled")
    assert any(snap.url == "https://mock.local/home" for snap, _ in collected)


@pytest.mark.asyncio
async def test_invalid_action_json_does_not_crash():
    llm = _MockLLM([])
    llm._responses = ["not valid json", json.dumps({"action": "done"})]
    explorer = Explorer(llm=llm)
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/home", _HOME_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/home",
            marketplace="kijiji",
            query="aeron",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert result.stop_reason == "done"
    assert len(collected) == 1


@pytest.mark.asyncio
async def test_scroll_with_new_content_does_not_stall():
    """Infinite-scroll page: scrolling reveals new content -> no stall stop.

    Regression test for the v13 infinite-scroll false-positive: URL never
    changes, but content does, so the explorer must keep going until the
    LLM says done."""
    html = """<html><body>
      <div style="height:2000px">listing block one</div>
      <div style="height:2000px">listing block two</div>
      <div style="height:2000px">listing block three</div>
      </body></html>"""
    llm = _MockLLM([
        {"action": "scroll"},
        {"action": "scroll"},
        {"action": "scroll"},
        {"action": "done", "reason": "no_results after full scroll"},
    ])
    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/serp", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/serp",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    assert result.stop_reason == "done"


@pytest.mark.asyncio
async def test_four_no_effect_actions_stops_stalled():
    """Actions that change nothing (URL, scroll, content all static)
    must hard-stop with 'stalled' after STALL_THRESHOLD consecutive."""
    html = "<html><body><p>static page, nothing to click</p></body></html>"
    # Clicking a nonexistent id changes nothing, 4x in a row.
    llm = _MockLLM([{"action": "click", "id": 99999}] * 6)
    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/static", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/static",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    assert result.stop_reason == "stalled"
    assert result.turns_used < Explorer.MAX_TURNS


@pytest.mark.asyncio
async def test_premature_done_gets_nudged_then_accepted():
    """A low-coverage done with a non-exempt reason is rejected twice
    (nudges), then accepted on the third attempt."""
    html = "<html><body><p>results page</p></body></html>"
    llm = _MockLLM([
        {"action": "done", "reason": "looks finished"},
        {"action": "done", "reason": "really finished"},
        {"action": "done", "reason": "still finished"},
    ])
    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/serp", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/serp",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    assert result.stop_reason == "done"
    assert result.turns_used == 3          # 2 nudges consumed + accepted


@pytest.mark.asyncio
async def test_exempt_done_reason_accepted_immediately():
    html = "<html><body><p>login wall</p></body></html>"
    llm = _MockLLM([{"action": "done", "reason": "auth_wall on this site"}])
    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/login", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/login",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    assert result.stop_reason == "done"
    assert result.turns_used == 1
