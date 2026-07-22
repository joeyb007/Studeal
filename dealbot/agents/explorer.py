"""Explorer — LLM-driven marketplace browsing sub-agent.

    Given a marketplace's HOME URL + a search query, the Explorer runs a
    bounded ReAct loop where the LLM decides how to use the site's UI —
    find the search bar, type the query, submit, then paginate through
    results via "Next" controls or scrolling.

    The Explorer does NOT extract listings. Every unique URL it lands on
    is captured as a snapshot and pushed to a sink (typically an
    ExtractorPool). Extraction happens downstream, in parallel with
    continued browsing.

Contract:
    - Tool set: navigate, click, type, scroll, done.
        - `click` / `type` act on element ids from the perception
          snapshot's [id] brackets, resolved via element_map →
          CDP-native click (bbox fallback). No role/name matching.
        - `type` clicks the element, types the text, presses Enter.
    - Auto-enqueue: when the URL changes since the previous turn, the
      previous URL's final snapshot is submitted to the sink. At end of
      loop, the current URL's final snapshot is submitted.
    - Bounded: MAX_TURNS caps the loop.
    - Stall detection: STALL_THRESHOLD consecutive actions with no visible
      effect (URL, scroll position, and page text all unchanged) → stop early.
    - Coverage-enforced done: low-coverage done calls are nudged (max
      MAX_NUDGES) unless the reason is captcha / auth_wall / no_results.
    - Zero per-site adapter code. Everything (search bar location,
      pagination controls, etc.) is discovered via the AX-tree snapshot.

Rationale: solves the v13 context-rot bug by removing extraction from
the ReAct context. Explorer's context is small (nav decisions only),
which lets it run to 20 turns without drift. Starting from the home
page — rather than a pre-built SERP URL — removes the last per-site
adapter (URL templates).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from playwright.async_api import Page
from pydantic import BaseModel, ValidationError

from dealbot.agents.perception import PageSnapshot, snapshot_page, truncate_snapshot_text
from dealbot.agents.tools import try_cdp_native_click
from dealbot.agents.tracing import NullTraceWriter, TraceWriter
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import BrowserSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Settled-snapshot helper
# ---------------------------------------------------------------------------

_MIN_SETTLED_CHARS = 800
_MIN_SETTLED_ELEMS = 25
_SETTLE_RETRIES = 2
_SETTLE_DELAY_S = 1.5


async def _settled_snapshot(page: Page) -> PageSnapshot:
    """Snapshot, retrying briefly when the page looks like an unrendered
    JS shell (seen on eBay: 399-char snapshot of a page that renders to
    137k chars ~1s later). Site-agnostic: emptiness, not domain, triggers it."""
    snap = await snapshot_page(page)
    for _ in range(_SETTLE_RETRIES):
        if len(snap.text) >= _MIN_SETTLED_CHARS or len(snap.element_map) >= _MIN_SETTLED_ELEMS:
            break
        await asyncio.sleep(_SETTLE_DELAY_S)
        snap = await snapshot_page(page)
    return snap


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExplorerResult:
    urls_visited: list[str] = field(default_factory=list)  # deduped, insertion order
    turns_used: int = 0
    stop_reason: str = "done"        # "done" | "max_turns" | "stalled" | "error"


# ---------------------------------------------------------------------------
# Sink type
# ---------------------------------------------------------------------------

# async def sink(snap: PageSnapshot, marketplace: str) -> None
Sink = Callable[[PageSnapshot, str], Awaitable[None]]


# ---------------------------------------------------------------------------
# LLM response shape
# ---------------------------------------------------------------------------

class _ActionJSON(BaseModel):
    action: str
    url: str | None = None            # navigate
    id: int | None = None             # click / type — element id from snapshot
    text: str | None = None           # type
    reason: str | None = None         # done


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXPLORER_SYSTEM = """You are a marketplace browsing sub-agent. You land on a
marketplace's home page and your job is to reach + paginate the search-result
pages for a user's query, then stop. You do NOT extract listings — a separate
worker handles that downstream by processing every page snapshot you leave
behind.

Your goal is COVERAGE: land on as many distinct search-result URLs as possible.

The page snapshot shows interactive elements in [id] brackets, e.g.:
  [43]<a href="..." /> "Next"
  [17]<input type="text" /> "Search Kijiji"
Act on elements by their id. Ids change every turn — always use ids from the
CURRENT snapshot.

Tools (emit ONE per turn as JSON):
  {"action":"type","id":17,"text":"<query>"}
      Click element 17, type the text, press Enter. Use on the search box.
  {"action":"click","id":43}
      Click element 43. Use for "Next"/">" pagination links, "Load more"
      buttons, or dismissing cookie/consent banners.
  {"action":"scroll"}
      Scroll down one viewport — reveals below-fold results on infinite-scroll
      sites and pagination controls at the bottom of result pages.
  {"action":"navigate","url":"https://..."}
      Go directly to a URL. Rare — prefer driving the site's own UI.
  {"action":"done","reason":"..."}
      Stop. Premature done calls are rejected — cover multiple result pages
      before stopping unless you hit a captcha, auth wall, or no_results
      (say which in your reason).

Standard flow:
  1. You land on the marketplace home page.
  2. Turn 1: type the query into the search box (submits and navigates to
     results). Dismiss any cookie/consent banner first if one is shown.
  3. Then: scroll to reveal pagination, click next-page controls to walk
     through result pages.
  4. Do NOT click into individual listing cards.
  5. Do NOT revisit URLs you've already landed on.
  6. When you've paginated the current query fully, call done."""


# ---------------------------------------------------------------------------
# Explorer
# ---------------------------------------------------------------------------

# Reasons that allow a `done` call to bypass the coverage check.
EXEMPT_DONE_REASONS: tuple[str, ...] = ("captcha", "auth_wall", "no_results")


class Explorer:
    MAX_TURNS: int = 20
    STALL_THRESHOLD: int = 4   # N consecutive no-effect actions → stop
    MIN_SERP_URLS: int = 2     # minimum distinct SERP URLs (beyond entry) before done is accepted
    MIN_PAGINATION_ATTEMPTS: int = 3  # minimum scroll/click actions before done is accepted
    MAX_NUDGES: int = 2        # maximum rejection nudges before accepting done unconditionally

    def __init__(self, llm: LLMClient, trace: TraceWriter | None = None) -> None:
        self.llm = llm
        self.trace = trace or NullTraceWriter()

    async def explore(
        self,
        entry_url: str,
        marketplace: str,
        query: str,
        spec: WatchlistContext,
        session: BrowserSession,
        sink: Sink,
    ) -> ExplorerResult:
        # Auto-navigate to the marketplace home page
        try:
            await session.page.goto(
                entry_url, wait_until="domcontentloaded", timeout=20_000,
            )
        except Exception as exc:
            logger.warning("Explorer: initial goto failed for %r: %s", entry_url, exc)
            self.trace.record_error(orchestrator_turn=0, worker="explorer", error=str(exc)[:300])
            return ExplorerResult(
                urls_visited=[], turns_used=0, stop_reason="error",
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": EXPLORER_SYSTEM},
            {"role": "user", "content": _initial_prompt(marketplace, entry_url, query, spec)},
        ]

        # State carried across turns
        seen_urls: list[str] = []
        seen_url_set: set[str] = set()
        prev_url: str | None = None
        prev_snap: PageSnapshot | None = None
        prev_key: tuple | None = None
        no_effect_streak: int = 0
        pagination_attempts: int = 0   # scroll + click actions dispatched
        nudges_used: int = 0           # coverage-rejection nudges sent to LLM
        failed_action_streak: int = 0  # consecutive click/type failures

        for turn in range(self.MAX_TURNS):
            snap = await _settled_snapshot(session.page)

            # When URL changes since last turn, submit the previous URL's
            # snapshot to the sink (final observed state of that URL).
            if prev_url is not None and snap.url != prev_url and prev_snap is not None:
                await sink(prev_snap, marketplace)

            # Track unique URLs.
            if snap.url and snap.url not in seen_url_set:
                seen_url_set.add(snap.url)
                seen_urls.append(snap.url)

            # Stall detection: if URL, scroll position, and recent page text
            # are all unchanged, increment the no-effect streak.
            try:
                scroll_y = int(await session.page.evaluate("() => window.scrollY"))
            except Exception:
                scroll_y = 0
            key = _stall_key(snap, scroll_y)
            if prev_key is not None and key == prev_key:
                no_effect_streak += 1
                if no_effect_streak >= self.STALL_THRESHOLD:
                    if snap.url:
                        await sink(snap, marketplace)
                    return ExplorerResult(
                        urls_visited=seen_urls,
                        turns_used=turn,
                        stop_reason="stalled",
                    )
            else:
                no_effect_streak = 0
            prev_key = key

            prev_url = snap.url
            prev_snap = snap

            # Build per-turn prompt with current snapshot state.
            turn_text = _turn_prompt(
                snap=snap, turn=turn, max_turns=self.MAX_TURNS,
                urls_visited=seen_urls, query=query,
                no_effect_streak=no_effect_streak,
            )
            want_vision = (
                getattr(self.llm, "supports_vision", False)
                and (failed_action_streak >= 2 or snap.captcha_detected)
            )
            if want_vision:
                try:
                    png = await session.page.screenshot()
                except Exception:
                    png = None
                if png:
                    self.trace.record_screenshot(
                        orchestrator_turn=0, sub_turn=turn,
                        label="vision_fallback", png_bytes=png,
                    )
                    messages.append({"role": "user", "content": [
                        {"type": "text", "text": turn_text},
                        {"type": "image_url", "image_url": {"url":
                            "data:image/png;base64,"
                            + base64.b64encode(png).decode()}},
                    ]})
                else:
                    messages.append({"role": "user", "content": turn_text})
            else:
                messages.append({"role": "user", "content": turn_text})

            # Capture the conversation sent to the LLM before calling it,
            # redacting inline base64 images to avoid duplicating PNG data already
            # saved via record_screenshot.
            prompt_snapshot = _redact_images(messages)

            # LLM emits an action.
            try:
                response = await self.llm.complete(
                    messages, response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("Explorer: LLM call failed on turn %d: %s", turn, exc)
                self.trace.record_error(orchestrator_turn=0, worker="explorer", error=str(exc)[:300])
                if prev_snap and prev_snap.url:
                    await sink(prev_snap, marketplace)
                return ExplorerResult(
                    urls_visited=seen_urls,
                    turns_used=turn,
                    stop_reason="error",
                )

            messages.append({"role": "assistant", "content": response.content})
            messages = _trim_ephemeral(messages)
            self.trace.record_page_reader_turn(
                orchestrator_turn=0,
                sub_turn=turn,
                url=snap.url,
                snapshot_text=snap.text,
                element_map_size=len(snap.element_map),
                prompt=prompt_snapshot,
                response_content=response.content,
                action_summary=response.content[:200] if response.content else "",
                result_summary="",
            )

            action, err = _parse_action(response.content)
            if err is not None:
                messages.append({"role": "user", "content": (
                    f"Invalid action JSON: {err}. Re-emit."
                )})
                continue

            action_type = action.action

            if action_type == "done":
                reason = (action.reason or "").lower()
                exempt = any(k in reason for k in EXEMPT_DONE_REASONS)
                enough = (
                    len(seen_urls) >= self.MIN_SERP_URLS + 1
                    or pagination_attempts >= self.MIN_PAGINATION_ATTEMPTS
                )
                if not exempt and not enough and nudges_used < self.MAX_NUDGES:
                    # Nudge turns dispatch no action, so they count toward the
                    # stall streak — MAX_NUDGES must stay < STALL_THRESHOLD - 1
                    # or an all-nudge session would stall-exit before accepting.
                    nudges_used += 1
                    messages.append({"role": "user", "content": (
                        "Coverage is too low to stop: you have visited "
                        f"{max(len(seen_urls) - 1, 0)} result page(s) and made "
                        f"{pagination_attempts} pagination attempt(s). Keep going — "
                        "scroll for more results or click the next-page control. "
                        "Only stop now for a captcha, auth wall, or a page with "
                        "no results (say so in your reason)."
                    )})
                    continue
                if prev_snap and prev_snap.url:
                    await sink(prev_snap, marketplace)
                return ExplorerResult(
                    urls_visited=seen_urls,
                    turns_used=turn + 1,
                    stop_reason="done",
                )

            if action_type == "scroll":
                try:
                    await session.page.evaluate(
                        "() => window.scrollBy(0, window.innerHeight)"
                    )
                    result_msg = "Scrolled one viewport."
                except Exception as exc:
                    result_msg = f"Scroll failed: {type(exc).__name__}"
                pagination_attempts += 1
                messages.append({"role": "user", "content": result_msg})
                continue

            if action_type == "navigate":
                url = action.url or ""
                if not url:
                    messages.append({"role": "user", "content": (
                        "navigate requires 'url' field. Re-emit."
                    )})
                    continue
                try:
                    await session.page.goto(
                        url, wait_until="domcontentloaded", timeout=20_000,
                    )
                    result_msg = f"Navigated to {url}."
                except Exception as exc:
                    result_msg = f"Navigate to {url} failed: {type(exc).__name__}."
                messages.append({"role": "user", "content": result_msg})
                continue

            if action_type == "click":
                if action.id is None:
                    messages.append({"role": "user", "content":
                        "click requires 'id' (an [id] from the snapshot). Re-emit."})
                    continue
                result_msg, ok = await _do_click(session.page, session, snap, action.id)
                failed_action_streak = 0 if ok else failed_action_streak + 1
                # Only successful clicks count toward the done-coverage gate —
                # failed clicks never touched the page, so they aren't coverage.
                if ok:
                    pagination_attempts += 1
                messages.append({"role": "user", "content": result_msg})
                continue

            if action_type == "type":
                if action.id is None or action.text is None:
                    messages.append({"role": "user", "content":
                        "type requires 'id' and 'text'. Re-emit."})
                    continue
                result_msg, ok = await _do_type(
                    session.page, session, snap, action.id, action.text,
                )
                failed_action_streak = 0 if ok else failed_action_streak + 1
                messages.append({"role": "user", "content": result_msg})
                continue

            # Unknown action.
            messages.append({"role": "user", "content": (
                f"Action {action_type!r} is unknown. Valid: navigate, click, "
                "type, scroll, done."
            )})

        # Turn budget exhausted.
        if prev_snap and prev_snap.url:
            await sink(prev_snap, marketplace)
        return ExplorerResult(
            urls_visited=seen_urls,
            turns_used=self.MAX_TURNS,
            stop_reason="max_turns",
        )


# ---------------------------------------------------------------------------
# Interaction helpers
# ---------------------------------------------------------------------------

async def _do_click(
    page: Page, session: BrowserSession, snap: PageSnapshot, element_id: int,
) -> tuple[str, bool]:
    elem = snap.element_map.get(element_id)
    if elem is None:
        return (
            f"click: id {element_id} is not in the current snapshot. "
            "Use an [id] shown in this turn's snapshot.", False,
        )
    url_before = page.url
    clicked = await try_cdp_native_click(page, elem.backend_node_id)
    if not clicked:
        if elem.bbox is None:
            return (f"click: element [{element_id}] unreachable (no bbox).", False)
        x, y, w, h = elem.bbox
        try:
            await page.mouse.click(x + w / 2, y + h / 2)
        except Exception as exc:
            return (f"click failed: {type(exc).__name__}: {exc}", False)
    try:
        await session.watchdog.wait_for_settlement(
            after_action="click", timeout_ms=5000,
        )
    except Exception:
        pass
    if page.url != url_before:
        return (f"Clicked [{element_id}] {elem.name!r}; now at {page.url}", True)
    return (f"Clicked [{element_id}] {elem.name!r}; URL unchanged.", True)


async def _do_type(
    page: Page, session: BrowserSession, snap: PageSnapshot,
    element_id: int, text: str,
) -> tuple[str, bool]:
    elem = snap.element_map.get(element_id)
    if elem is None or elem.bbox is None:
        return (
            f"type: id {element_id} is not in the current snapshot "
            "(or has no position). Use an [id] from this turn's snapshot.", False,
        )
    x, y, w, h = elem.bbox
    url_before = page.url
    try:
        await page.mouse.click(x + w / 2, y + h / 2)
        await page.keyboard.type(text)
        await page.keyboard.press("Enter")
    except Exception as exc:
        return (f"type failed: {type(exc).__name__}: {exc}", False)
    try:
        await session.watchdog.wait_for_settlement(
            after_action="type", timeout_ms=5000,
        )
    except Exception:
        pass
    if page.url != url_before:
        return (f"Typed {text!r}; submitted, now at {page.url}", True)
    return (
        f"Typed {text!r} but the URL did not change. If a search suggestion "
        "dropdown appeared, click its first result; otherwise click the "
        "site's search button by [id].", False,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _initial_prompt(
    marketplace: str, entry_url: str, query: str, spec: WatchlistContext,
) -> str:
    return (
        f"Marketplace: {marketplace}\n"
        f"Home URL (already loaded): {entry_url}\n"
        f"Query to search for: {query!r}\n"
        f"Watchlist spec: {spec.model_dump_json(indent=2)}\n\n"
        "You are on the marketplace home page. Your first action is typically "
        "to type the query into the site's search bar. Emit the first action."
    )


def _turn_prompt(
    snap: PageSnapshot,
    turn: int,
    max_turns: int,
    urls_visited: list[str],
    query: str,
    no_effect_streak: int = 0,
) -> str:
    text = truncate_snapshot_text(snap.text)
    visited_block = (
        "\n".join(f"  - {u}" for u in urls_visited[-8:])
        if urls_visited else "  (none yet)"
    )
    captcha_block = (
        "\n⚠ CAPTCHA / bot-challenge detected. Call done with reason='captcha'.\n"
        if snap.captcha_detected else ""
    )
    stall_block = (
        f"\n⚠ Your last {no_effect_streak} action(s) had no visible effect "
        f"(page unchanged). {Explorer.STALL_THRESHOLD - no_effect_streak} more "
        "and the run is stopped. Try a different action.\n"
        if no_effect_streak > 0 else ""
    )
    return (
        f"Turn {turn + 1}/{max_turns}.\n"
        f"Query to search for: {query!r}\n"
        f"Current URL: {snap.url}\n"
        f"Recently visited URLs (unique):\n{visited_block}\n"
        f"{captcha_block}"
        f"{stall_block}"
        f"\nPage snapshot:\n{text}\n\n"
        "Emit the next action as JSON."
    )


def _parse_action(content: str | None) -> tuple[_ActionJSON | None, str | None]:
    # content can be None when the LLM returns an empty response (seen after
    # SSL-retry recovery) — must nudge, not crash the exploration.
    if not isinstance(content, str) or not content.strip():
        return None, "empty response"
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        return None, f"JSON: {exc}"
    if not isinstance(data, dict):
        return None, "top-level must be a JSON object"
    if "action" not in data:
        return None, "missing 'action' field"
    try:
        return _ActionJSON.model_validate(data), None
    except ValidationError as exc:
        return None, f"validation: {exc.errors()[:1]}"


def _redact_images(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return a copy of messages with base64 image_url blocks replaced by a text placeholder."""
    result = []
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            new_content = []
            for block in content:
                if block.get("type") == "image_url":
                    data_url = (block.get("image_url") or {}).get("url", "")
                    new_content.append({
                        "type": "text",
                        "text": f"<screenshot omitted from trace: {len(data_url)} chars base64>",
                    })
                else:
                    new_content.append(block)
            result.append({**msg, "content": new_content})
        else:
            result.append(msg)
    return result


def _stall_key(snap: PageSnapshot, scroll_y: int) -> tuple:
    return (snap.url, scroll_y, hash(snap.text[-2000:]))


def _is_turn_message(m: dict[str, Any]) -> bool:
    if m.get("role") != "user":
        return False
    content = m.get("content", "")
    if isinstance(content, list):
        content = next(
            (b.get("text", "") for b in content if b.get("type") == "text"), "",
        )
    return str(content).startswith("Turn ")


def _trim_ephemeral(
    messages: list[dict[str, Any]], keep_last_n: int = 2,
) -> list[dict[str, Any]]:
    """Keep system + initial user + last N per-turn user messages."""
    if len(messages) <= 2:
        return messages
    head = messages[:2]
    tail = messages[2:]
    snapshot_indices = [
        i for i, m in enumerate(tail)
        if _is_turn_message(m)
    ]
    drop = set(snapshot_indices[:-keep_last_n]) if len(snapshot_indices) > keep_last_n else set()
    trimmed_tail = [m for i, m in enumerate(tail) if i not in drop]
    return head + trimmed_tail
