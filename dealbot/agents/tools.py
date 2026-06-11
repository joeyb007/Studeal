"""Browser tools for the PageReader subagent.

Each tool is a `BrowserTool` subclass exposing a Pydantic action schema (the
JSON schema the LLM sees) + an `execute(action, ctx) → ActionResult`.

Behavior shared by every mutating tool (`navigate`, `click`, `type`, `scroll`):
  1. Snapshot the page before the action (for change_summary diff).
  2. Execute the action via Playwright/CDP.
  3. `await ctx.session.watchdog.wait_for_settlement(after_action=...)` — event-
     driven, never `asyncio.sleep`.
  4. Snapshot after.
  5. Compute change_summary (page_changed bool + element-id diff).
  6. Return ActionResult with success flag + diff. PageReader uses the diff to
     verify whether the action visibly did something — silent failures show up
     as `page_changed: False`.

NavigateTool also enforces per-domain rate limiting (min 2s between hits to the
same domain) to avoid scraper-like access patterns getting us blocked.

Phase 1.7 (stretch) will wire `take_screenshot` to a real VLM. v1 stubs it:
logs `(url, reason)` to OrchestratorState.vision_fallback_log and returns empty.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, ClassVar, Literal
from urllib.parse import urlparse

from playwright.async_api import Page
from pydantic import BaseModel, Field

from dealbot.agents.perception import PageSnapshot, snapshot_page
from dealbot.agents.state import (
    Finding,
    OrchestratorState,
    Provenance,
    Thread,
    VisionFallbackEntry,
)
from dealbot.scrapers.browser_session import BrowserSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Denylist for safety — bare-bones; full URL-safety policy is the orchestrator's
# job. Tool layer just refuses obviously dangerous navigations.
# ---------------------------------------------------------------------------

_NAV_DENYLIST = (
    "localhost", "127.0.0.1", "0.0.0.0",
    "10.", "172.16.", "172.17.", "172.18.", "172.19.", "172.20.",
    "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
    "172.27.", "172.28.", "172.29.", "172.30.", "172.31.",
    "192.168.",
)
_DOMAIN_MIN_INTERVAL_S = 2.0
_SETTLEMENT_TIMEOUT_MS = 5000


# ---------------------------------------------------------------------------
# Action types (Pydantic — schema becomes the LLM-facing tool definition)
# ---------------------------------------------------------------------------

class NavigateAction(BaseModel):
    type: Literal["navigate"] = "navigate"
    url: str


class ClickAction(BaseModel):
    type: Literal["click"] = "click"
    element_id: int
    fallback_name: str | None = None    # semantic targeting hedge


class TypeAction(BaseModel):
    type: Literal["type"] = "type"
    element_id: int
    text: str
    submit: bool = False


class ScrollAction(BaseModel):
    type: Literal["scroll"] = "scroll"
    direction: Literal["up", "down"] = "down"
    amount: int = 1                     # in pages


class ReadPageAction(BaseModel):
    type: Literal["read_page"] = "read_page"


class RecordFindingAction(BaseModel):
    type: Literal["record_finding"] = "record_finding"
    text: str
    provenance: Provenance
    source_url: str | None = None


class SpawnLeadAction(BaseModel):
    type: Literal["spawn_lead"] = "spawn_lead"
    intent: str
    url: str


class TakeScreenshotAction(BaseModel):
    type: Literal["take_screenshot"] = "take_screenshot"
    question: str                        # what the LLM wants the VLM to answer


class DoneAction(BaseModel):
    type: Literal["done"] = "done"
    reason: str


Action = (
    NavigateAction | ClickAction | TypeAction | ScrollAction | ReadPageAction
    | RecordFindingAction | SpawnLeadAction | TakeScreenshotAction | DoneAction
)


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------

class ChangeSummary(BaseModel):
    """Diff of element_map between before/after a mutating action.

    `page_changed` is the action-verification signal: if False, the click/type
    didn't visibly do anything → silent failure, surfaced to PageReader so it
    can pick a different approach.
    """

    page_changed: bool
    url_before: str
    url_after: str
    elements_before: int
    elements_after: int
    new_element_ids: list[int] = Field(default_factory=list)
    gone_element_ids: list[int] = Field(default_factory=list)


ErrorType = Literal["timeout", "not_found", "detached", "blocked", "denylist", "validation"]


class ActionError(BaseModel):
    error_type: ErrorType
    retriable: bool
    message: str


class ActionResult(BaseModel):
    """What a tool's `execute` returns. PageReader inspects this to decide
    its next move."""

    model_config = {"arbitrary_types_allowed": True}

    success: bool
    error: ActionError | None = None
    change_summary: ChangeSummary | None = None
    snapshot: Any | None = None       # PageSnapshot when applicable
    payload: dict[str, Any] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Context handed to every tool's execute()
# ---------------------------------------------------------------------------

@dataclass
class ToolContext:
    page: Page
    session: BrowserSession
    state: OrchestratorState
    current_thread: Thread
    rate_limiter: "DomainRateLimiter"
    turn: int


# ---------------------------------------------------------------------------
# Per-domain rate limiter
# ---------------------------------------------------------------------------

class DomainRateLimiter:
    """Tracks per-domain last-visit time and sleeps before the next hit."""

    def __init__(self, min_interval_s: float = _DOMAIN_MIN_INTERVAL_S) -> None:
        self._min = min_interval_s
        self._last_at: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, url: str) -> None:
        domain = _domain_of(url)
        if not domain:
            return
        async with self._lock:
            now = time.monotonic()
            last = self._last_at.get(domain, 0.0)
            wait = self._min - (now - last)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_at[domain] = time.monotonic()


def _domain_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Snapshot diff
# ---------------------------------------------------------------------------

async def _try_cdp_native_click(page: Page, backend_node_id: int) -> bool:
    """Attempt a CDP-native click via DOM.scrollIntoViewIfNeeded + fresh box
    model + Input.dispatchMouseEvent.

    Advantages over `page.mouse.click(x, y)` with the snapshot bbox:
      - Scrolls the element into view first (avoids "below the fold" misses)
      - Uses fresh post-scroll coordinates from CDP (not stale snapshot data)
      - Handles iframe / shadow DOM coordinate translation natively
      - Dispatches real mousePressed + mouseReleased pairs (matches what a
        human mouse generates; survives some anti-bot fingerprinting)

    Returns True on success. False if CDP isn't available (e.g. in unit-test
    mocks) OR any step fails — caller then falls back to pixel-mouse click.
    """
    try:
        cdp = await page.context.new_cdp_session(page)
    except Exception:
        return False
    try:
        # 1. Scroll into view. Tolerate failures here — element may already
        # be in view, and scrollIntoViewIfNeeded is best-effort.
        try:
            await cdp.send(
                "DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_node_id},
            )
        except Exception:
            pass

        # 2. Fresh box model post-scroll.
        try:
            box_data = await cdp.send(
                "DOM.getBoxModel", {"backendNodeId": backend_node_id},
            )
        except Exception:
            return False
        model = box_data.get("model")
        if not model:
            return False
        # border is a flat quad [x1,y1, x2,y2, x3,y3, x4,y4] of the visible
        # border edges. Centroid = mean of the 4 corners.
        border = model.get("border", [])
        if len(border) < 8:
            return False
        cx = sum(border[0::2]) / 4
        cy = sum(border[1::2]) / 4

        # 3. Real mouse-event pair at fresh centroid.
        try:
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mousePressed",
                "x": cx, "y": cy,
                "button": "left",
                "clickCount": 1,
            })
            await cdp.send("Input.dispatchMouseEvent", {
                "type": "mouseReleased",
                "x": cx, "y": cy,
                "button": "left",
                "clickCount": 1,
            })
        except Exception:
            return False
        return True
    finally:
        try:
            await cdp.detach()
        except Exception:
            pass


def diff_snapshots(before: PageSnapshot, after: PageSnapshot) -> ChangeSummary:
    """Compute a ChangeSummary between two PageSnapshots."""
    before_ids = set(before.element_map.keys())
    after_ids = set(after.element_map.keys())
    new_ids = sorted(after_ids - before_ids)
    gone_ids = sorted(before_ids - after_ids)
    url_changed = before.url != after.url
    significant_element_change = (len(new_ids) + len(gone_ids)) >= 3
    return ChangeSummary(
        page_changed=url_changed or significant_element_change,
        url_before=before.url,
        url_after=after.url,
        elements_before=len(before_ids),
        elements_after=len(after_ids),
        new_element_ids=new_ids[:20],
        gone_element_ids=gone_ids[:20],
    )


# ---------------------------------------------------------------------------
# Tool ABC
# ---------------------------------------------------------------------------

class BrowserTool(ABC):
    """Subclasses set `name` and `action_model`. `execute` does the work."""

    name: ClassVar[str]
    action_model: ClassVar[type[BaseModel]]

    @abstractmethod
    async def execute(self, action: Any, ctx: ToolContext) -> ActionResult:
        ...


# ---------------------------------------------------------------------------
# Mutating tools
# ---------------------------------------------------------------------------

class NavigateTool(BrowserTool):
    name: ClassVar[str] = "navigate"
    action_model: ClassVar[type[BaseModel]] = NavigateAction

    async def execute(self, action: NavigateAction, ctx: ToolContext) -> ActionResult:
        url = action.url.strip()
        if not url.startswith(("http://", "https://")):
            return ActionResult(success=False, error=ActionError(
                error_type="validation", retriable=False,
                message=f"navigate: expected http(s) URL, got {url!r}",
            ))
        domain = _domain_of(url)
        if any(domain.startswith(b) for b in _NAV_DENYLIST):
            return ActionResult(success=False, error=ActionError(
                error_type="denylist", retriable=False,
                message=f"navigate: domain {domain!r} on denylist",
            ))

        await ctx.rate_limiter.acquire(url)
        before = await snapshot_page(ctx.page)
        try:
            await ctx.page.goto(url, wait_until="domcontentloaded", timeout=20_000)
        except Exception as exc:
            return ActionResult(success=False, error=ActionError(
                error_type="timeout", retriable=True, message=str(exc)[:200],
            ))
        await ctx.session.watchdog.wait_for_settlement(
            after_action="navigate", timeout_ms=_SETTLEMENT_TIMEOUT_MS,
        )
        after = await snapshot_page(ctx.page)
        return ActionResult(
            success=True,
            change_summary=diff_snapshots(before, after),
            snapshot=after,
        )


class ClickTool(BrowserTool):
    name: ClassVar[str] = "click"
    action_model: ClassVar[type[BaseModel]] = ClickAction

    async def execute(self, action: ClickAction, ctx: ToolContext) -> ActionResult:
        before = await snapshot_page(ctx.page)
        elem = before.element_map.get(action.element_id)
        if elem is None and action.fallback_name:
            # Semantic-targeting hedge: search current snapshot by name.
            for candidate in before.element_map.values():
                if candidate.is_interactive and candidate.name == action.fallback_name:
                    elem = candidate
                    break
        if elem is None or elem.bbox is None:
            return ActionResult(success=False, error=ActionError(
                error_type="not_found", retriable=False,
                message=f"click: element_id {action.element_id} not in snapshot",
            ))

        x, y, w, h = elem.bbox
        cx, cy = x + w / 2, y + h / 2
        try:
            await ctx.page.mouse.click(cx, cy)
        except Exception as exc:
            return ActionResult(success=False, error=ActionError(
                error_type="detached", retriable=True, message=str(exc)[:200],
            ))
        await ctx.session.watchdog.wait_for_settlement(
            after_action="click", timeout_ms=_SETTLEMENT_TIMEOUT_MS,
        )
        after = await snapshot_page(ctx.page)
        return ActionResult(
            success=True,
            change_summary=diff_snapshots(before, after),
            snapshot=after,
        )


class TypeTool(BrowserTool):
    name: ClassVar[str] = "type"
    action_model: ClassVar[type[BaseModel]] = TypeAction

    async def execute(self, action: TypeAction, ctx: ToolContext) -> ActionResult:
        before = await snapshot_page(ctx.page)
        elem = before.element_map.get(action.element_id)
        if elem is None or elem.bbox is None:
            return ActionResult(success=False, error=ActionError(
                error_type="not_found", retriable=False,
                message=f"type: element_id {action.element_id} not in snapshot",
            ))
        x, y, w, h = elem.bbox
        cx, cy = x + w / 2, y + h / 2
        try:
            await ctx.page.mouse.click(cx, cy)
            await ctx.page.keyboard.type(action.text)
            if action.submit:
                await ctx.page.keyboard.press("Enter")
        except Exception as exc:
            return ActionResult(success=False, error=ActionError(
                error_type="detached", retriable=True, message=str(exc)[:200],
            ))
        await ctx.session.watchdog.wait_for_settlement(
            after_action="type", timeout_ms=_SETTLEMENT_TIMEOUT_MS,
        )
        after = await snapshot_page(ctx.page)
        return ActionResult(
            success=True,
            change_summary=diff_snapshots(before, after),
            snapshot=after,
        )


class ScrollTool(BrowserTool):
    name: ClassVar[str] = "scroll"
    action_model: ClassVar[type[BaseModel]] = ScrollAction

    async def execute(self, action: ScrollAction, ctx: ToolContext) -> ActionResult:
        before = await snapshot_page(ctx.page)
        delta = action.amount * (800 if action.direction == "down" else -800)
        try:
            await ctx.page.mouse.wheel(0, delta)
        except Exception as exc:
            return ActionResult(success=False, error=ActionError(
                error_type="timeout", retriable=True, message=str(exc)[:200],
            ))
        await ctx.session.watchdog.wait_for_settlement(
            after_action="scroll", timeout_ms=_SETTLEMENT_TIMEOUT_MS,
        )
        after = await snapshot_page(ctx.page)
        return ActionResult(
            success=True,
            change_summary=diff_snapshots(before, after),
            snapshot=after,
        )


# ---------------------------------------------------------------------------
# Read-only / state-mutating tools
# ---------------------------------------------------------------------------

class ReadPageTool(BrowserTool):
    name: ClassVar[str] = "read_page"
    action_model: ClassVar[type[BaseModel]] = ReadPageAction

    async def execute(self, action: ReadPageAction, ctx: ToolContext) -> ActionResult:
        snap = await snapshot_page(ctx.page)
        return ActionResult(success=True, snapshot=snap)


class RecordFindingTool(BrowserTool):
    name: ClassVar[str] = "record_finding"
    action_model: ClassVar[type[BaseModel]] = RecordFindingAction

    async def execute(self, action: RecordFindingAction, ctx: ToolContext) -> ActionResult:
        finding = Finding(
            text=action.text,
            provenance=action.provenance,
            source_url=action.source_url or ctx.page.url,
        )
        ctx.current_thread.findings.append(finding)
        return ActionResult(
            success=True,
            payload={"finding_count": len(ctx.current_thread.findings)},
        )


class SpawnLeadTool(BrowserTool):
    name: ClassVar[str] = "spawn_lead"
    action_model: ClassVar[type[BaseModel]] = SpawnLeadAction

    async def execute(self, action: SpawnLeadAction, ctx: ToolContext) -> ActionResult:
        # PageReader collects spawned leads; orchestrator scores + pushes onto
        # frontier on return. We stash a placeholder finding so it survives
        # the dispatch summary.
        ctx.current_thread.findings.append(Finding(
            text=f"[lead] {action.intent} → {action.url}",
            provenance="inference",
            source_url=ctx.page.url,
        ))
        return ActionResult(
            success=True,
            payload={"intent": action.intent, "url": action.url},
        )


class TakeScreenshotTool(BrowserTool):
    """Stub for v1 — logs to vision_fallback_log so we know which URLs would
    benefit from a real VLM. Phase 1.7 stretch wires this to a deployed model."""

    name: ClassVar[str] = "take_screenshot"
    action_model: ClassVar[type[BaseModel]] = TakeScreenshotAction

    async def execute(self, action: TakeScreenshotAction, ctx: ToolContext) -> ActionResult:
        ctx.state.vision_fallback_log.append(VisionFallbackEntry(
            url=ctx.page.url,
            reason=action.question[:200],
            turn=ctx.turn,
        ))
        return ActionResult(
            success=True,
            payload={
                "stub": True,
                "answer": "[take_screenshot stub: VLM not yet deployed. "
                          "Try DOM-based perception or a different approach.]",
            },
        )


class DoneTool(BrowserTool):
    """Terminal — PageReader's loop reads this result and exits."""

    name: ClassVar[str] = "done"
    action_model: ClassVar[type[BaseModel]] = DoneAction

    async def execute(self, action: DoneAction, ctx: ToolContext) -> ActionResult:
        return ActionResult(
            success=True,
            payload={"reason": action.reason, "done": True},
        )


# ---------------------------------------------------------------------------
# Registry — composed in dealbot/agents/composition.py
# ---------------------------------------------------------------------------

def all_tools() -> list[BrowserTool]:
    """The 9 tools PageReader's subagent loop dispatches over."""
    return [
        NavigateTool(),
        ClickTool(),
        TypeTool(),
        ScrollTool(),
        ReadPageTool(),
        RecordFindingTool(),
        SpawnLeadTool(),
        TakeScreenshotTool(),
        DoneTool(),
    ]
