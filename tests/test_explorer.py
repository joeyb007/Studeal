"""Tests for Explorer — LLM-driven marketplace browsing sub-agent.

Uses real LocalPlaywrightSession + Playwright route.fulfill for URL mocks,
mocked LLM with pre-queued action responses.
"""

from __future__ import annotations

import json
import re

import pytest

from dealbot.agents.explorer import Explorer, ExplorerResult, _redact_images
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
        self.supports_vision = False

    async def complete(self, messages, response_format=None, **kwargs):
        self.calls += 1
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse(json.dumps({"action": "done", "reason": "test-fallback"}))


def _id_for(turn_prompt: str, label: str) -> int:
    """Extract the [id] of the element whose accessible name is `label`
    from a snapshot block like: [43]<a href="..." /> "Next"."""
    m = re.search(rf'\[(\d+)\]<[^>]*>[^"\n]*"{re.escape(label)}"', turn_prompt)
    assert m, f"no element labeled {label!r} in prompt"
    return int(m.group(1))


class _DynamicLLM:
    """Like _MockLLM, but entries may be callables(last_user_text) -> dict,
    so scripted actions can reference element ids discovered at runtime."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    async def complete(self, messages, response_format=None, **kwargs):
        entry = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        last_user = next(
            m["content"] for m in reversed(messages)
            if m["role"] == "user" and isinstance(m["content"], str)
        )
        action = entry(last_user) if callable(entry) else entry
        return _MockResponse(json.dumps(action))


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



_FILLER = "".join(
    f'<a href="#f{i}">Filler item {i} — realistic page weight</a> ' for i in range(60)
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
  <div>{FILLER}</div>
</body></html>
""".replace("{FILLER}", _FILLER)

_SERP_HTML = """
<!doctype html>
<html><head><title>Marketplace SERP</title></head><body>
  <h1>Search results</h1>
  <div>Listing A</div>
  <div>Listing B</div>
  <a href="https://mock.local/page2">Next</a>
  <div>{FILLER}</div>
</body></html>
""".replace("{FILLER}", _FILLER)

_PAGE2_HTML = """
<!doctype html>
<html><head><title>Marketplace P2</title></head><body>
  <h1>Search results — page 2</h1>
  <div>Listing C</div>
  <div>{FILLER}</div>
</body></html>
""".replace("{FILLER}", _FILLER)


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
    llm = _DynamicLLM([
        lambda p: {"action": "type", "id": _id_for(p, "Search"), "text": "aeron"},
        {"action": "done", "reason": "no_results"},
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
    llm = _DynamicLLM([
        lambda p: {"action": "click", "id": _id_for(p, "Next")},
        {"action": "done", "reason": "no_results"},
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
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/",
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
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/",
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
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/",
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
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        result = await Explorer(llm).explore(
            entry_url="https://example.test/",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    assert result.stop_reason == "done"
    assert result.turns_used == 1


@pytest.mark.asyncio
async def test_explorer_writes_trace(tmp_path):
    from dealbot.agents.tracing import FilesystemTraceWriter
    html = "<html><body><p>page</p></body></html>"
    llm = _MockLLM([{"action": "done", "reason": "no_results"}])
    trace = FilesystemTraceWriter(tmp_path / "run1")
    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        await Explorer(llm, trace=trace).explore(
            entry_url="https://example.test/",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    trace.finalize()
    assert (tmp_path / "run1" / "trace.jsonl").exists()
    assert (tmp_path / "run1" / "trace.jsonl").read_text().strip()


@pytest.mark.asyncio
async def test_vision_fallback_after_two_failed_actions():
    """Two consecutive failed actions on a vision-capable LLM -> the next
    turn's user message is multimodal (text + screenshot)."""
    html = "<html><body><p>page</p></body></html>"
    llm = _MockLLM([
        {"action": "click", "id": 99999},   # fails: id not in snapshot
        {"action": "click", "id": 99998},   # fails again
        {"action": "done", "reason": "no_results"},
    ])
    llm.supports_vision = True
    captured: list = []
    original = llm.complete

    async def capturing(messages, **kw):
        captured.append([dict(m) for m in messages])
        return await original(messages, **kw)
    llm.complete = capturing

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://example.test/", html)
        sink, seen = await _sink_collector()
        await Explorer(llm).explore(
            entry_url="https://example.test/",
            marketplace="test", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
    third_turn_msgs = captured[2]
    last_user = next(m for m in reversed(third_turn_msgs) if m["role"] == "user")
    assert isinstance(last_user["content"], list)
    assert any(b.get("type") == "image_url" for b in last_user["content"])


def test_trace_prompt_redacts_screenshots():
    """_redact_images replaces image_url blocks in copies, leaving originals intact."""
    fake_data_url = "data:image/png;base64," + "A" * 100
    messages = [
        {"role": "user", "content": "plain string message"},
        {"role": "user", "content": [
            {"type": "text", "text": "describe this"},
            {"type": "image_url", "image_url": {"url": fake_data_url}},
        ]},
    ]

    redacted = _redact_images(messages)

    # (a) returned multimodal content has no image_url type blocks
    multimodal_out = redacted[1]["content"]
    assert isinstance(multimodal_out, list)
    assert not any(b.get("type") == "image_url" for b in multimodal_out), (
        "image_url block must be replaced in the redacted copy"
    )
    replacement = next(b for b in multimodal_out if b.get("type") == "text" and "omitted" in b.get("text", ""))
    assert str(len(fake_data_url)) in replacement["text"], (
        "replacement text must include the char count of the original data URL"
    )

    # (b) original messages list still contains the image_url block untouched
    orig_content = messages[1]["content"]
    assert any(b.get("type") == "image_url" for b in orig_content), (
        "_redact_images must not mutate the original messages"
    )
    assert orig_content[1]["image_url"]["url"] == fake_data_url, (
        "original image_url data must be preserved unchanged"
    )

    # (c) string-content message passes through identically
    assert redacted[0] is messages[0] or redacted[0] == messages[0], (
        "string-content messages should pass through unchanged"
    )
    assert redacted[0]["content"] == "plain string message"


def test_parse_action_handles_none_and_empty_content():
    """LLM responses with content=None (seen after SSL-retry recovery) must
    nudge, not crash. Regression: reliability_run_4 TypeError."""
    from dealbot.agents.explorer import _parse_action

    for bad in (None, "", "   "):
        action, err = _parse_action(bad)
        assert action is None
        assert err == "empty response"


@pytest.mark.asyncio
async def test_settled_snapshot_waits_for_js_render():
    """_settled_snapshot retries when the initial snapshot is a tiny JS shell.

    The mock page renders nearly empty at load; a setTimeout fills the body
    ~600ms later. The LLM immediately says done(no_results). We assert that
    the snapshot the sink receives contains "Loaded" — proving the retry
    waited for the JS to fire before the snapshot was accepted.
    """
    _JS_RENDER_HTML = """<!doctype html>
<html><head><title>Lazy</title></head>
<body>
<script>
  setTimeout(function() {
    document.body.innerHTML = '<h1>Loaded</h1>'
      + '<a href="#">item</a>'.repeat(50);
  }, 600);
</script>
</body></html>"""

    llm = _MockLLM([{"action": "done", "reason": "no_results"}])
    sink, collected = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/lazy", _JS_RENDER_HTML)
        await Explorer(llm=llm).explore(
            entry_url="https://mock.local/lazy",
            marketplace="test",
            query="aeron",
            spec=_spec(),
            session=bs,
            sink=sink,
        )

    assert collected, "sink must have received at least one snapshot"
    texts = [snap.text for snap, _ in collected]
    assert any("Loaded" in t for t in texts), (
        "_settled_snapshot must have retried until the JS-rendered content appeared"
    )


# ---------------------------------------------------------------------
# A1: session warm-up entry + domain guardrail
# ---------------------------------------------------------------------

@pytest.mark.asyncio
async def test_warmup_visits_root_before_template_entry():
    """Entry on a deep SERP URL first loads the site root (session warm-up),
    then the SERP. Guards against soft-blocks on cookie-less deep links."""
    llm = _MockLLM([{"action": "done", "reason": "no_results on this page"}])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()
    visited: list[str] = []

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        await _mock_page(bs, "https://mock.local/search?q=aeron", _SERP_HTML)
        bs.page.on("framenavigated", lambda frame: visited.append(frame.url))

        result = await explorer.explore(
            entry_url="https://mock.local/search?q=aeron",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )

    assert result.stop_reason == "done"
    assert visited[0].rstrip("/") == "https://mock.local"
    assert any("search?q=aeron" in u for u in visited[1:])


@pytest.mark.asyncio
async def test_entry_on_root_skips_warmup():
    """Entry that already IS the root does not double-load it."""
    llm = _MockLLM([{"action": "done", "reason": "no_results"}])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()
    visited: list[str] = []

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        bs.page.on("framenavigated", lambda frame: visited.append(frame.url))
        await explorer.explore(
            entry_url="https://mock.local/",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )

    assert [u for u in visited if u.rstrip("/") == "https://mock.local"] != []
    assert len(visited) == 1


@pytest.mark.asyncio
async def test_navigate_off_domain_is_blocked():
    """The LLM asking to navigate off the entry marketplace's registrable
    domain is refused with an explanatory message; the page never moves."""
    llm = _MockLLM([
        {"action": "navigate", "url": "https://www.evil.example/login"},
        {"action": "done", "reason": "no_results"},
    ])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
        assert bs.page.url.startswith("https://mock.local")

    assert result.stop_reason == "done"


@pytest.mark.asyncio
async def test_navigate_to_subdomain_is_allowed():
    """Subdomain hops within the registrable domain stay legal
    (toronto.craigslist.org ↔ craigslist.org)."""
    llm = _MockLLM([
        {"action": "navigate", "url": "https://sub.mock.local/area"},
        {"action": "done", "reason": "no_results"},
    ])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        await _mock_page(bs, "https://sub.mock.local/area", _SERP_HTML)
        await explorer.explore(
            entry_url="https://mock.local/",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
        assert bs.page.url.startswith("https://sub.mock.local")


# ---------------------------------------------------------------------
# A2: degenerate-entry fallback
# ---------------------------------------------------------------------

_DEAD_HTML = """
<!doctype html>
<html><head><title>Error Page</title></head><body>
  <p>SORRY</p><p>Something went wrong on our end</p>
  <a href="https://mock.local/">Go to homepage</a>
</body></html>
"""


@pytest.mark.asyncio
async def test_degenerate_entry_falls_back_to_home_search_ui():
    """A dead SERP (error shell) triggers re-entry via the site root, with the
    initial prompt telling the LLM to use the site's own search box."""
    class _CapturingLLM(_MockLLM):
        def __init__(self, actions):
            super().__init__(actions)
            self.user_messages: list[str] = []

        async def complete(self, messages, response_format=None, **kwargs):
            self.user_messages = [
                m["content"] for m in messages
                if m["role"] == "user" and isinstance(m["content"], str)
            ]
            return await super().complete(messages, response_format, **kwargs)

    llm = _CapturingLLM([{"action": "done", "reason": "no_results"}])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        await _mock_page(bs, "https://mock.local/search?q=aeron", _DEAD_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/search?q=aeron",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
        assert bs.page.url.rstrip("/") == "https://mock.local"

    assert result.stop_reason == "done"
    assert llm.calls == 1
    joined = "\n".join(llm.user_messages)
    assert "search box" in joined.lower()


@pytest.mark.asyncio
async def test_degenerate_root_errors_without_llm_calls():
    """If the root is dead too, the site is walled: error out, spend nothing."""
    llm = _MockLLM([])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _DEAD_HTML)
        await _mock_page(bs, "https://mock.local/search?q=aeron", _DEAD_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/search?q=aeron",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )

    assert result.stop_reason == "error"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_healthy_entry_skips_fallback():
    """A healthy SERP entry never triggers the fallback path."""
    llm = _MockLLM([{"action": "done", "reason": "no_results"}])
    explorer = Explorer(llm=llm)
    sink, _ = await _sink_collector()

    async with LocalPlaywrightSession() as bs:
        await _mock_page(bs, "https://mock.local/", _HOME_HTML)
        await _mock_page(bs, "https://mock.local/search?q=aeron", _SERP_HTML)
        result = await explorer.explore(
            entry_url="https://mock.local/search?q=aeron",
            marketplace="mock", query="aeron", spec=_spec(),
            session=bs, sink=sink,
        )
        assert "search?q=aeron" in bs.page.url

    assert result.stop_reason == "done"
