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
        - `type` fills a role-based textbox and presses Enter (submits
          the surrounding form). Used to drive the marketplace's own
          search UI from the home page.
        - `click` uses Playwright's get_by_role(role, name=...) with a
          case-insensitive word-boundary match on the accessible name.
    - Auto-enqueue: when the URL changes since the previous turn, the
      previous URL's final snapshot is submitted to the sink. At end of
      loop, the current URL's final snapshot is submitted.
    - Bounded: MAX_TURNS caps the loop.
    - Stall detection: STALL_THRESHOLD consecutive actions with no visible
      effect (URL, scroll position, and page text all unchanged) → stop early.
    - Zero per-site adapter code. Everything (search bar location,
      pagination controls, etc.) is discovered via accessibility roles.

Rationale: solves the v13 context-rot bug by removing extraction from
the ReAct context. Explorer's context is small (nav decisions only),
which lets it run to 20 turns without drift. Starting from the home
page — rather than a pre-built SERP URL — removes the last per-site
adapter (URL templates).
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
    role: str | None = None           # click / type ("link" | "button" | "textbox" | "searchbox")
    name: str | None = None           # click / type accessible-name
    text: str | None = None           # type — the text to fill
    reason: str | None = None         # done


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

EXPLORER_SYSTEM = """You are a marketplace browsing sub-agent. You land on a
marketplace's home page and your job is to reach + paginate the search-result
pages for a user's query, then stop. You do NOT extract listings — a separate
worker handles that downstream by processing every page snapshot you leave
behind.

Your goal is COVERAGE: land on as many distinct SERP URLs as possible.

Tools (emit ONE per turn as JSON):
  {"action":"type","role":"textbox","name":"Search","text":"<query>"}
      Fill a search-bar textbox with the query and press Enter. Used at the
      start of exploration to submit the query via the site's own search UI.
      The 'name' field is a case-insensitive substring match against the
      textbox's accessible name — passing "Search" catches "Search Kijiji",
      "Search for products", etc.
  {"action":"click","role":"link","name":"Next"}
      Click a control identified by its accessibility role + accessible name.
      Common patterns: role="link" name="Next" or "Next Page", role="button"
      name="Load more" or ">" or "→". Word-boundary case-insensitive match.
  {"action":"scroll"}
      Scroll down one viewport. Use to reveal below-fold content on
      infinite-scroll marketplaces, or to see the pagination controls at
      the bottom of a SERP.
  {"action":"navigate","url":"https://..."}
      Navigate directly to a URL. Rarely needed since you drive the site's
      own UI. Reserve for edge cases (following a specific category link).
  {"action":"done","reason":"..."}
      Stop exploring. Use when: pagination exhausted, page shows no listings,
      or you've hit a CAPTCHA / auth wall.

Standard flow:
  1. You land on the marketplace home page.
  2. Turn 1: type the query into the search bar (this submits the form and
     navigates to the SERP).
  3. Subsequent turns: scroll to reveal pagination controls, click "Next" /
     ">" style controls to walk through result pages.
  4. Do NOT click into individual listing cards — those become detail pages,
     which the extractor doesn't need.
  5. Do NOT revisit URLs you've already landed on.
  6. When you've paginated the current query fully, call done."""


# ---------------------------------------------------------------------------
# Explorer
# ---------------------------------------------------------------------------

class Explorer:
    MAX_TURNS: int = 20
    STALL_THRESHOLD: int = 4   # N consecutive no-effect actions → stop

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

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
            messages.append({"role": "user", "content": _turn_prompt(
                snap=snap, turn=turn, max_turns=self.MAX_TURNS,
                urls_visited=seen_urls, query=query,
                no_effect_streak=no_effect_streak,
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

            if action_type == "type":
                if not action.role or not action.name or action.text is None:
                    messages.append({"role": "user", "content": (
                        "type requires 'role', 'name', and 'text'. Re-emit."
                    )})
                    continue
                result_msg = await _try_type(
                    session.page, action.role, action.name, action.text,
                )
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

async def _try_click(page: Page, role: str, name: str) -> str:
    """Click by role + accessible name. Word-boundary case-insensitive match
    on `name` — 'Next' matches 'Next Page' but not 'Nexus'."""
    if role not in ("link", "button"):
        return f"click role={role!r} not supported; use 'link' or 'button'."
    try:
        pattern = re.compile(
            rf"\b{re.escape(name.strip())}\b", re.IGNORECASE,
        )
        locator = page.get_by_role(role, name=pattern)  # type: ignore[arg-type]
        count = await locator.count()
        if count == 0:
            return (
                f"No {role} with accessible name matching {name!r} found."
            )
        await locator.first.click(timeout=5000)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=10_000)
        except Exception:
            pass
        return f"Clicked {role} {name!r}."
    except Exception as exc:
        return f"Click failed: {type(exc).__name__}: {exc}"


async def _try_type(page: Page, role: str, name: str, text: str) -> str:
    """Fill a textbox by role + accessible name, then aggressively submit.

    Three fallback layers because marketplace search UIs are inconsistent:
        1. `locator.press("Enter")` — works on plain HTML forms.
        2. `form.submit()` via JS — works when Enter is JS-intercepted but
           the form has a native submit handler.
        3. Click a nearby "Search"/"Go"/"Find" button — works on fully
           JS-driven search widgets (Kijiji, some FB flows).

    Substring match on `name` (search bars vary in labelling: 'Search',
    'Search Kijiji', 'Search for products'). Reports URL delta so the LLM
    can detect a failed submit and try a different approach.
    """
    if role not in ("textbox", "searchbox"):
        return f"type role={role!r} not supported; use 'textbox' or 'searchbox'."
    try:
        pattern = re.compile(re.escape(name.strip()), re.IGNORECASE)
        locator = page.get_by_role(role, name=pattern)  # type: ignore[arg-type]
        count = await locator.count()
        if count == 0:
            return f"No {role} with accessible name matching {name!r} found."
        first = locator.first
        url_before = page.url
        await first.click(timeout=3000)
        await first.fill(text, timeout=3000)

        # Layer 1: press Enter
        await first.press("Enter", timeout=3000)
        if await _wait_for_navigation(page, url_before, timeout_ms=3000):
            return f"Typed {text!r}; Enter submitted, now at {page.url}"

        # Layer 2: submit the enclosing form via JS
        try:
            submitted = await first.evaluate(
                "el => { const f = el.closest('form'); "
                "if (f) { f.submit(); return true; } return false; }"
            )
            if submitted and await _wait_for_navigation(page, url_before, timeout_ms=3000):
                return f"Typed {text!r}; form.submit() submitted, now at {page.url}"
        except Exception:
            pass

        # Layer 3: click a nearby search button
        for btn_name in ("Search", "Go", "Find", "Submit"):
            try:
                btn = page.get_by_role(
                    "button",
                    name=re.compile(rf"\b{re.escape(btn_name)}\b", re.IGNORECASE),
                )
                if await btn.count() == 0:
                    continue
                await btn.first.click(timeout=2000)
                if await _wait_for_navigation(page, url_before, timeout_ms=5000):
                    return (
                        f"Typed {text!r}; clicked {btn_name} button, now at {page.url}"
                    )
            except Exception:
                continue

        return (
            f"Typed {text!r} but URL did not change from {url_before}. "
            "The site may require a category selection first, or the search "
            "UI is JS-only. Try navigate to a direct search URL instead."
        )
    except Exception as exc:
        return f"Type failed: {type(exc).__name__}: {exc}"


async def _wait_for_navigation(page: Page, url_before: str, timeout_ms: int) -> bool:
    """Return True if URL changed from `url_before` within timeout_ms."""
    try:
        await page.wait_for_url(lambda u: u != url_before, timeout=timeout_ms)
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass
        return True
    except Exception:
        return False


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
    text = snap.text if len(snap.text) <= 18000 else snap.text[:18000] + "\n[...truncated]"
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


def _stall_key(snap: PageSnapshot, scroll_y: int) -> tuple:
    return (snap.url, scroll_y, hash(snap.text[-2000:]))


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
        if m.get("role") == "user" and str(m.get("content", "")).startswith("Turn ")
    ]
    drop = set(snapshot_indices[:-keep_last_n]) if len(snapshot_indices) > keep_last_n else set()
    trimmed_tail = [m for i, m in enumerate(tail) if i not in drop]
    return head + trimmed_tail
