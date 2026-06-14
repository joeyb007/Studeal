"""Tests for dealbot.agents.tools.

Strategy:
  - Mock Page / Mouse / Keyboard via record-and-replay objects
  - Monkeypatch snapshot_page to return canned PageSnapshots
  - For each tool: assert it does what it claims AND emits the right
    ActionResult shape (success, error type, change_summary)
  - Action verification: a click with no element change → page_changed=False
  - DomainRateLimiter: second call within min_interval_s is delayed
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from dealbot.agents.perception import ElementRef, PageSnapshot
from dealbot.agents.state import (
    OrchestratorState,
    Thread,
)
from dealbot.agents.tools import (
    ActionResult,
    ChangeSummary,
    ClickAction,
    ClickTool,
    DomainRateLimiter,
    DoneAction,
    DoneTool,
    NavigateAction,
    NavigateTool,
    ReadPageAction,
    ReadPageTool,
    RecordFindingAction,
    RecordFindingTool,
    ScrollAction,
    ScrollTool,
    SpawnLeadAction,
    SpawnLeadTool,
    TakeScreenshotAction,
    TakeScreenshotTool,
    ToolContext,
    TypeAction,
    TypeTool,
    diff_snapshots,
)
from dealbot.schemas import WatchlistContext


# ---------------------------------------------------------------------------
# Mocks
# ---------------------------------------------------------------------------

class _MockMouse:
    def __init__(self) -> None:
        self.clicks: list[tuple[float, float]] = []
        self.wheel_calls: list[tuple[int, int]] = []
        self.click_raises: Exception | None = None

    async def click(self, x: float, y: float) -> None:
        if self.click_raises is not None:
            raise self.click_raises
        self.clicks.append((x, y))

    async def wheel(self, dx: int, dy: int) -> None:
        self.wheel_calls.append((dx, dy))


class _MockKeyboard:
    def __init__(self) -> None:
        self.typed: list[str] = []
        self.pressed: list[str] = []

    async def type(self, text: str) -> None:
        self.typed.append(text)

    async def press(self, key: str) -> None:
        self.pressed.append(key)


class _MockPage:
    def __init__(self, url: str = "https://example.com/start") -> None:
        self.url = url
        self.mouse = _MockMouse()
        self.keyboard = _MockKeyboard()
        self.gotos: list[str] = []
        self.goto_raises: Exception | None = None

    async def goto(self, url: str, **kwargs: Any) -> None:
        if self.goto_raises is not None:
            raise self.goto_raises
        self.gotos.append(url)
        self.url = url

    async def title(self) -> str:
        return "mock"


class _MockWatchdog:
    def __init__(self) -> None:
        self.settle_calls: list[str] = []

    async def wait_for_settlement(self, after_action: str = "", **kwargs: Any) -> None:
        self.settle_calls.append(after_action)


class _MockSession:
    def __init__(self) -> None:
        self.watchdog = _MockWatchdog()
        self.intercepted_responses: list = []


def _make_snapshot(url: str, ids: list[int]) -> PageSnapshot:
    """Build a PageSnapshot with the given element IDs as interactive elements."""
    element_map = {
        i: ElementRef(
            backend_node_id=i,
            role="button",
            name=f"btn-{i}",
            tag_name="button",
            bbox=(10.0 + i * 60, 10.0, 50.0, 30.0),
            is_interactive=True,
        )
        for i in ids
    }
    return PageSnapshot(
        text=f"<mock for {url}>",
        element_map=element_map,
        url=url,
        title="mock",
        char_count=20,
    )


def _make_ctx(page: _MockPage, snapshots: list[PageSnapshot] | None = None) -> tuple[
    ToolContext, list[PageSnapshot]
]:
    """Build a ToolContext + the list snapshot_page will pop from."""
    spec = WatchlistContext(product_query="sample", keywords=["sample"])
    state = OrchestratorState(spec=spec)
    thread = Thread(id="t1", intent="test", current_url=page.url)
    state.current_thread = thread
    ctx = ToolContext(
        page=page,                  # type: ignore[arg-type]
        session=_MockSession(),     # type: ignore[arg-type]
        state=state,
        current_thread=thread,
        rate_limiter=DomainRateLimiter(min_interval_s=0.01),
        turn=1,
    )
    return ctx, list(snapshots or [])


def _install_snapshot_queue(monkeypatch, queue: list[PageSnapshot]) -> None:
    """Replace snapshot_page in the tools module so it pops from `queue`."""
    async def fake_snapshot(page: Any) -> PageSnapshot:
        if not queue:
            raise AssertionError("snapshot_page called more times than fixtures provided")
        return queue.pop(0)
    monkeypatch.setattr("dealbot.agents.tools.snapshot_page", fake_snapshot)


# ---------------------------------------------------------------------------
# Diff helper
# ---------------------------------------------------------------------------

def test_diff_no_change_when_identical():
    s = _make_snapshot("https://a/", [1, 2, 3])
    diff = diff_snapshots(s, s)
    assert diff.page_changed is False
    assert diff.new_element_ids == []
    assert diff.gone_element_ids == []


def test_diff_detects_url_change():
    a = _make_snapshot("https://a/", [1, 2, 3])
    b = _make_snapshot("https://b/", [1, 2, 3])
    diff = diff_snapshots(a, b)
    assert diff.page_changed is True
    assert diff.url_before == "https://a/"
    assert diff.url_after == "https://b/"


def test_diff_detects_significant_element_change():
    a = _make_snapshot("https://a/", [1, 2, 3])
    b = _make_snapshot("https://a/", [1, 2, 3, 10, 11, 12])  # 3 new
    diff = diff_snapshots(a, b)
    assert diff.page_changed is True
    assert diff.new_element_ids == [10, 11, 12]


def test_diff_ignores_minor_element_change():
    a = _make_snapshot("https://a/", [1, 2, 3])
    b = _make_snapshot("https://a/", [1, 2, 3, 10])  # only 1 new — below threshold
    diff = diff_snapshots(a, b)
    assert diff.page_changed is False


# ---------------------------------------------------------------------------
# NavigateTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_navigate_rejects_non_http_url():
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    result = await NavigateTool().execute(NavigateAction(url="ftp://nope.com"), ctx)
    assert not result.success
    assert result.error.error_type == "validation"


@pytest.mark.asyncio
async def test_navigate_rejects_denylisted_domain():
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    result = await NavigateTool().execute(NavigateAction(url="http://localhost:8000"), ctx)
    assert not result.success
    assert result.error.error_type == "denylist"


@pytest.mark.asyncio
async def test_navigate_happy_path(monkeypatch):
    page = _MockPage(url="https://before/")
    ctx, _ = _make_ctx(page)
    _install_snapshot_queue(monkeypatch, [
        _make_snapshot("https://before/", [1]),
        _make_snapshot("https://amazon.ca/", [1, 2, 3, 4]),
    ])
    result = await NavigateTool().execute(
        NavigateAction(url="https://amazon.ca/"), ctx,
    )
    assert result.success
    assert page.gotos == ["https://amazon.ca/"]
    assert ctx.session.watchdog.settle_calls == ["navigate"]
    assert result.change_summary.page_changed is True


@pytest.mark.asyncio
async def test_navigate_timeout_returns_retriable_error(monkeypatch):
    page = _MockPage()
    page.goto_raises = asyncio.TimeoutError("timeout")
    ctx, _ = _make_ctx(page)
    _install_snapshot_queue(monkeypatch, [_make_snapshot("https://before/", [1])])
    result = await NavigateTool().execute(
        NavigateAction(url="https://amazon.ca/"), ctx,
    )
    assert not result.success
    assert result.error.error_type == "timeout"
    assert result.error.retriable is True


# ---------------------------------------------------------------------------
# ClickTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_click_unknown_element_returns_not_found(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    _install_snapshot_queue(monkeypatch, [_make_snapshot("https://a/", [1, 2])])
    result = await ClickTool().execute(ClickAction(element_id=99), ctx)
    assert not result.success
    assert result.error.error_type == "not_found"


@pytest.mark.asyncio
async def test_click_happy_path_emits_change_summary(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    _install_snapshot_queue(monkeypatch, [
        _make_snapshot("https://a/", [1, 2, 3]),
        _make_snapshot("https://a/result", [10, 11, 12, 13]),
    ])
    result = await ClickTool().execute(ClickAction(element_id=2), ctx)
    assert result.success
    assert page.mouse.clicks   # one click recorded
    assert ctx.session.watchdog.settle_calls == ["click"]
    assert result.change_summary.page_changed is True


@pytest.mark.asyncio
async def test_click_no_page_change_flagged(monkeypatch):
    """Action verification: click that doesn't visibly change the page."""
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1, 2, 3])
    _install_snapshot_queue(monkeypatch, [s, s])  # identical before/after
    result = await ClickTool().execute(ClickAction(element_id=1), ctx)
    assert result.success                       # the click executed
    assert result.change_summary.page_changed is False  # but nothing visible happened


@pytest.mark.asyncio
async def test_click_fallback_name_resolution(monkeypatch):
    """If element_id is stale, fallback_name should locate by AX name."""
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [5])
    # element_id 99 doesn't exist, but element 5's name is "btn-5"
    _install_snapshot_queue(monkeypatch, [s, s])
    result = await ClickTool().execute(
        ClickAction(element_id=99, fallback_name="btn-5"), ctx,
    )
    assert result.success
    assert page.mouse.clicks      # the fallback resolved to element 5


# ---------------------------------------------------------------------------
# CDP-native click path
# ---------------------------------------------------------------------------

class _ClickCdpSession:
    """A CDP session mock specialized for the click path. Records every send()."""

    def __init__(
        self,
        *,
        box_border: list[float] | None = None,
        getbox_raises: Exception | None = None,
        dispatch_raises: Exception | None = None,
    ) -> None:
        self.sent: list[tuple[str, dict | None]] = []
        self.box_border = box_border
        self.getbox_raises = getbox_raises
        self.dispatch_raises = dispatch_raises
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> Any:
        self.sent.append((method, params))
        if method == "DOM.scrollIntoViewIfNeeded":
            return {}
        if method == "DOM.getBoxModel":
            if self.getbox_raises is not None:
                raise self.getbox_raises
            if self.box_border is None:
                return {}   # missing model → CDP path bails out
            return {"model": {"border": self.box_border}}
        if method == "Input.dispatchMouseEvent":
            if self.dispatch_raises is not None:
                raise self.dispatch_raises
            return {}
        return {}

    async def detach(self) -> None:
        self.detached = True


class _PageContext:
    def __init__(self, cdp: _ClickCdpSession) -> None:
        self._cdp = cdp

    async def new_cdp_session(self, page: Any) -> _ClickCdpSession:
        return self._cdp


def _attach_cdp(page: _MockPage, cdp: _ClickCdpSession) -> None:
    """Bolt a context onto the MockPage so the CDP path can be exercised."""
    page.context = _PageContext(cdp)   # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_click_uses_cdp_native_path_when_available(monkeypatch):
    """CDP-native click should fire scrollIntoView + getBoxModel + 2 mouse
    events; the pixel-mouse fallback should NOT be invoked."""
    page = _MockPage()
    cdp = _ClickCdpSession(box_border=[100, 50, 200, 50, 200, 80, 100, 80])
    _attach_cdp(page, cdp)
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [42])
    _install_snapshot_queue(monkeypatch, [s, s])

    result = await ClickTool().execute(ClickAction(element_id=42), ctx)
    assert result.success

    sent_methods = [m for m, _ in cdp.sent]
    assert "DOM.scrollIntoViewIfNeeded" in sent_methods
    assert "DOM.getBoxModel" in sent_methods
    assert sent_methods.count("Input.dispatchMouseEvent") == 2
    assert cdp.detached

    # CDP path succeeded → pixel-mouse fallback NOT used
    assert page.mouse.clicks == []


@pytest.mark.asyncio
async def test_click_uses_centroid_of_border_quad(monkeypatch):
    """The dispatched click coordinates should be the average of the 4 corners."""
    page = _MockPage()
    # Square quad: corners at (100,50), (200,50), (200,80), (100,80)
    # Centroid: (150, 65)
    cdp = _ClickCdpSession(box_border=[100, 50, 200, 50, 200, 80, 100, 80])
    _attach_cdp(page, cdp)
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [42])
    _install_snapshot_queue(monkeypatch, [s, s])

    await ClickTool().execute(ClickAction(element_id=42), ctx)

    dispatch_calls = [params for m, params in cdp.sent if m == "Input.dispatchMouseEvent"]
    assert dispatch_calls[0]["x"] == 150
    assert dispatch_calls[0]["y"] == 65
    assert dispatch_calls[0]["type"] == "mousePressed"
    assert dispatch_calls[1]["type"] == "mouseReleased"


@pytest.mark.asyncio
async def test_click_falls_back_to_mouse_when_getboxmodel_fails(monkeypatch):
    """If CDP getBoxModel raises, fall back to pixel mouse click on snapshot bbox."""
    page = _MockPage()
    cdp = _ClickCdpSession(getbox_raises=RuntimeError("no node"))
    _attach_cdp(page, cdp)
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [42])
    _install_snapshot_queue(monkeypatch, [s, s])

    result = await ClickTool().execute(ClickAction(element_id=42), ctx)
    assert result.success
    # Pixel-mouse fallback WAS used
    assert len(page.mouse.clicks) == 1


@pytest.mark.asyncio
async def test_click_falls_back_when_dispatch_fails(monkeypatch):
    """If CDP dispatch raises, fall back to pixel mouse click."""
    page = _MockPage()
    cdp = _ClickCdpSession(
        box_border=[100, 50, 200, 50, 200, 80, 100, 80],
        dispatch_raises=RuntimeError("disconnected"),
    )
    _attach_cdp(page, cdp)
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [42])
    _install_snapshot_queue(monkeypatch, [s, s])

    result = await ClickTool().execute(ClickAction(element_id=42), ctx)
    assert result.success
    assert len(page.mouse.clicks) == 1


# ---------------------------------------------------------------------------
# TypeTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_type_with_submit_presses_enter(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1])
    _install_snapshot_queue(monkeypatch, [s, s])
    result = await TypeTool().execute(
        TypeAction(element_id=1, text="hello", submit=True), ctx,
    )
    assert result.success
    assert page.keyboard.typed == ["hello"]
    assert page.keyboard.pressed == ["Enter"]


@pytest.mark.asyncio
async def test_type_without_submit_does_not_press_enter(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1])
    _install_snapshot_queue(monkeypatch, [s, s])
    await TypeTool().execute(TypeAction(element_id=1, text="hello"), ctx)
    assert page.keyboard.pressed == []


# ---------------------------------------------------------------------------
# ScrollTool
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scroll_down_positive_delta(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1])
    _install_snapshot_queue(monkeypatch, [s, s])
    await ScrollTool().execute(ScrollAction(direction="down", amount=2), ctx)
    dx, dy = page.mouse.wheel_calls[0]
    assert dy > 0


@pytest.mark.asyncio
async def test_scroll_up_negative_delta(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1])
    _install_snapshot_queue(monkeypatch, [s, s])
    await ScrollTool().execute(ScrollAction(direction="up", amount=1), ctx)
    dx, dy = page.mouse.wheel_calls[0]
    assert dy < 0


# ---------------------------------------------------------------------------
# Read-only / state-mutating tools
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_read_page_returns_snapshot(monkeypatch):
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    s = _make_snapshot("https://a/", [1, 2])
    _install_snapshot_queue(monkeypatch, [s])
    result = await ReadPageTool().execute(ReadPageAction(), ctx)
    assert result.success
    assert result.snapshot is s


@pytest.mark.asyncio
async def test_record_finding_appends_to_thread():
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    assert ctx.current_thread.findings == []
    result = await RecordFindingTool().execute(
        RecordFindingAction(
            text="Sony WH-1000XM5 = $199.99",
            provenance="observation",
            source_url="https://amazon.ca/dp/xyz",
        ),
        ctx,
    )
    assert result.success
    assert len(ctx.current_thread.findings) == 1
    assert ctx.current_thread.findings[0].provenance == "observation"
    assert ctx.current_thread.findings[0].source_url == "https://amazon.ca/dp/xyz"


@pytest.mark.asyncio
async def test_record_finding_defaults_source_url_to_current_page():
    page = _MockPage(url="https://current/")
    ctx, _ = _make_ctx(page)
    await RecordFindingTool().execute(
        RecordFindingAction(text="$50 off", provenance="observation"),
        ctx,
    )
    assert ctx.current_thread.findings[0].source_url == "https://current/"


@pytest.mark.asyncio
async def test_spawn_lead_records_marker_finding():
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    await SpawnLeadTool().execute(
        SpawnLeadAction(intent="check bestbuy", url="https://bestbuy.ca"),
        ctx,
    )
    assert len(ctx.current_thread.findings) == 1
    assert "[lead]" in ctx.current_thread.findings[0].text


@pytest.mark.asyncio
async def test_take_screenshot_logs_vision_fallback():
    page = _MockPage(url="https://canvas-page/")
    ctx, _ = _make_ctx(page)
    result = await TakeScreenshotTool().execute(
        TakeScreenshotAction(question="where is the price?"),
        ctx,
    )
    assert result.success
    assert result.payload["stub"] is True
    assert len(ctx.state.vision_fallback_log) == 1
    entry = ctx.state.vision_fallback_log[0]
    assert entry.url == "https://canvas-page/"
    assert "where is the price" in entry.reason


@pytest.mark.asyncio
async def test_done_tool_returns_done_payload():
    page = _MockPage()
    ctx, _ = _make_ctx(page)
    result = await DoneTool().execute(DoneAction(reason="enough"), ctx)
    assert result.success
    assert result.payload == {"reason": "enough", "done": True}


# ---------------------------------------------------------------------------
# DomainRateLimiter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rate_limiter_first_call_no_wait():
    rl = DomainRateLimiter(min_interval_s=0.1)
    start = asyncio.get_event_loop().time()
    await rl.acquire("https://a.com/path")
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.05


@pytest.mark.asyncio
async def test_rate_limiter_second_call_waits():
    rl = DomainRateLimiter(min_interval_s=0.1)
    await rl.acquire("https://a.com/")
    start = asyncio.get_event_loop().time()
    await rl.acquire("https://a.com/other")  # same domain → must wait
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed >= 0.08, f"expected ≥0.08s wait, got {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_rate_limiter_different_domains_independent():
    rl = DomainRateLimiter(min_interval_s=0.1)
    await rl.acquire("https://a.com/")
    start = asyncio.get_event_loop().time()
    await rl.acquire("https://b.com/")  # different domain → no wait
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.05
