"""Explorer — LLM-driven marketplace browsing sub-agent.

    Given a starting marketplace URL, the Explorer runs a bounded ReAct
    loop where the LLM decides how to navigate — click "Next" links,
    scroll infinite-scroll SPAs, or navigate directly to related URLs.

    The Explorer does NOT extract listings. Every unique URL it lands on
    is captured as a snapshot and pushed to a sink (typically an
    ExtractorPool). Extraction happens downstream, in parallel with
    continued browsing.

Contract:
    - Tool set: navigate, click, scroll, done. (No record_finding, no
      spawn_lead, no extraction.)
    - Auto-enqueue: when the URL changes since the previous turn, the
      previous URL's final snapshot is submitted to the sink. At end of
      loop, the current URL's final snapshot is submitted.
    - Bounded: MAX_TURNS caps the loop.
    - Loop detection: N consecutive identical snapshots → stop early.
    - Click uses Playwright's get_by_role(role, name=...) — matches on
      standardized accessibility patterns, zero per-site CSS selectors.

Rationale: solves the v13 context-rot bug by removing extraction from
the ReAct context. Explorer's context is small (nav decisions only),
which lets it run to 20 turns without drift.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from playwright.async_api import Page
from pydantic import BaseModel, ValidationError

from dealbot.agents.perception import PageSnapshot, snapshot_page
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import BrowserSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class ExplorerResult:
    urls_visited: list[str] = field(default_factory=list)  # deduped, insertion order
    turns_used: int = 0
    stop_reason: str = "done"        # "done" | "max_turns" | "loop" | "error"


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
    role: str | None = None           # click ("link" | "button")
    name: str | None = None           # click accessible-name
    reason: str | None = None         # done


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXPLORER_SYSTEM = """You are a marketplace browsing sub-agent. Your job is to
visit as many search-result and category pages as you can within your turn
budget. You do NOT extract listings — a separate worker handles that
downstream by processing every page snapshot you leave behind.

Your goal is COVERAGE: land on as many distinct URLs as possible that contain
listing cards for the user's spec.

Tools (emit ONE per turn as JSON):
  {"action":"scroll"}
      Scroll down one viewport. Use to reveal below-fold content on
      infinite-scroll marketplaces, or to see the pagination controls at
      the bottom of a SERP.
  {"action":"click","role":"link","name":"Next"}
      Click a control identified by its accessibility role + accessible name.
      Common patterns: role="link" name="Next" or "Next Page", role="button"
      name="Load more" or ">" or "→".
  {"action":"navigate","url":"https://..."}
      Navigate directly to a URL (only if you know a specific pagination URL).
  {"action":"done","reason":"..."}
      Stop exploring. Use when: pagination exhausted, page shows no listings,
      or you've hit a CAPTCHA / auth wall.

Strategy:
  1. Scroll to reveal below-fold content before hunting pagination controls.
  2. Prefer clicking "Next" or ">" style controls over navigating URLs directly.
  3. Do NOT click into individual listing cards — those become detail pages,
     which the extractor doesn't need.
  4. Do NOT revisit URLs you've already landed on.
  5. When you've paginated the current query fully, call done."""


# ---------------------------------------------------------------------------
# Explorer
# ---------------------------------------------------------------------------

class Explorer:
    MAX_TURNS: int = 20
    LOOP_DETECT_THRESHOLD: int = 3   # N consecutive identical snapshots → stop

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def explore(
        self,
        start_url: str,
        marketplace: str,
        spec: WatchlistContext,
        session: BrowserSession,
        sink: Sink,
    ) -> ExplorerResult:
        # Auto-navigate to start
        try:
            await session.page.goto(
                start_url, wait_until="domcontentloaded", timeout=20_000,
            )
        except Exception as exc:
            logger.warning("Explorer: initial goto failed for %r: %s", start_url, exc)
            return ExplorerResult(
                urls_visited=[], turns_used=0, stop_reason="error",
            )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": EXPLORER_SYSTEM},
            {"role": "user", "content": _initial_prompt(marketplace, start_url, spec)},
        ]

        # State carried across turns
        seen_urls: list[str] = []             # insertion order, deduped
        seen_url_set: set[str] = set()
        prev_url: str | None = None
        prev_snap: PageSnapshot | None = None
        recent_keys: list[tuple] = []

        for turn in range(self.MAX_TURNS):
            snap = await snapshot_page(session.page)

            # When URL changes since last turn, submit the previous URL's
            # snapshot to the sink (final observed state of that URL).
            if prev_url is not None and snap.url != prev_url and prev_snap is not None:
                await sink(prev_snap, marketplace)

            # Track unique URLs.
            if snap.url and snap.url not in seen_url_set:
                seen_url_set.add(snap.url)
                seen_urls.append(snap.url)

            # Loop detection.
            key = _snapshot_key(snap)
            if recent_keys and recent_keys[-1] == key:
                recent_keys.append(key)
                if len(recent_keys) >= self.LOOP_DETECT_THRESHOLD:
                    if snap.url:
                        await sink(snap, marketplace)
                    return ExplorerResult(
                        urls_visited=seen_urls,
                        turns_used=turn,
                        stop_reason="loop",
                    )
            else:
                recent_keys = [key]

            prev_url = snap.url
            prev_snap = snap

            # Build per-turn prompt with current snapshot state.
            messages.append({"role": "user", "content": _turn_prompt(
                snap=snap, turn=turn, max_turns=self.MAX_TURNS,
                urls_visited=seen_urls,
            )})

            # LLM emits an action.
            try:
                response = await self.llm.complete(
                    messages, response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("Explorer: LLM call failed on turn %d: %s", turn, exc)
                if prev_snap and prev_snap.url:
                    await sink(prev_snap, marketplace)
                return ExplorerResult(
                    urls_visited=seen_urls,
                    turns_used=turn,
                    stop_reason="error",
                )

            messages.append({"role": "assistant", "content": response.content})
            messages = _trim_ephemeral(messages)

            action, err = _parse_action(response.content)
            if err is not None:
                messages.append({"role": "user", "content": (
                    f"Invalid action JSON: {err}. Re-emit."
                )})
                continue

            action_type = action.action

            if action_type == "done":
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
                if not action.role or not action.name:
                    messages.append({"role": "user", "content": (
                        "click requires 'role' and 'name'. Re-emit."
                    )})
                    continue
                result_msg = await _try_click(
                    session.page, action.role, action.name,
                )
                messages.append({"role": "user", "content": result_msg})
                continue

            # Unknown action.
            messages.append({"role": "user", "content": (
                f"Action {action_type!r} is unknown. Valid: navigate, click, "
                "scroll, done."
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
# Click helper
# ---------------------------------------------------------------------------

async def _try_click(page: Page, role: str, name: str) -> str:
    """Click by role + accessible name. Returns a summary string for the LLM."""
    # Accept only role names Playwright supports for pagination-style controls.
    if role not in ("link", "button"):
        return f"click role={role!r} not supported; use 'link' or 'button'."
    try:
        # Case-insensitive exact-ish match on the accessible name.
        pattern = re.compile(f"^{re.escape(name.strip())}$", re.IGNORECASE)
        locator = page.get_by_role(role, name=pattern)  # type: ignore[arg-type]
        count = await locator.count()
        if count == 0:
            return (
                f"No {role} with accessible name {name!r} found on the page."
            )
        await locator.first.click(timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        return f"Clicked {role} {name!r}."
    except Exception as exc:
        return f"Click failed: {type(exc).__name__}: {exc}"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _initial_prompt(
    marketplace: str, start_url: str, spec: WatchlistContext,
) -> str:
    return (
        f"Marketplace: {marketplace}\n"
        f"Start URL: {start_url}\n"
        f"Watchlist spec: {spec.model_dump_json(indent=2)}\n\n"
        "You will now begin exploring. On each turn you'll see the current page "
        "snapshot and pick one action."
    )


def _turn_prompt(
    snap: PageSnapshot,
    turn: int,
    max_turns: int,
    urls_visited: list[str],
) -> str:
    text = snap.text if len(snap.text) <= 18000 else snap.text[:18000] + "\n[...truncated]"
    visited_block = (
        "\n".join(f"  - {u}" for u in urls_visited[-8:])
        if urls_visited else "  (none yet)"
    )
    captcha_block = (
        "\n⚠ CAPTCHA / bot-challenge detected. Call done with reason='captcha'.\n"
        if snap.captcha_detected else ""
    )
    return (
        f"Turn {turn + 1}/{max_turns}.\n"
        f"Current URL: {snap.url}\n"
        f"Recently visited URLs (unique):\n{visited_block}\n"
        f"{captcha_block}"
        f"\nPage snapshot:\n{text}\n\n"
        "Emit the next action as JSON."
    )


def _parse_action(content: str) -> tuple[_ActionJSON | None, str | None]:
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


def _snapshot_key(snap: PageSnapshot) -> tuple:
    ids = sorted(snap.element_map.keys())[:50]
    return (snap.url, tuple(ids))


def _trim_ephemeral(
    messages: list[dict[str, Any]], keep_last_n: int = 2,
) -> list[dict[str, Any]]:
    """Keep system + initial user + last N per-turn user messages.

    Same trimming strategy as v13 PageReader: user messages containing page
    snapshots grow the context linearly; drop older snapshot turns since the
    LLM's assistant reasoning still references what it did without needing the
    old snapshot text.
    """
    if len(messages) <= 2:
        return messages
    head = messages[:2]
    tail = messages[2:]
    snapshot_indices = [
        i for i, m in enumerate(tail)
        if m.get("role") == "user" and str(m.get("content", "")).startswith("Turn ")
    ]
    drop = set(snapshot_indices[:-keep_last_n]) if len(snapshot_indices) > keep_last_n else set()
    trimmed_tail = [m for i, m in enumerate(tail) if i not in drop]
    return head + trimmed_tail
