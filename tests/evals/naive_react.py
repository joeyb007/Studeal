"""Naive ReAct baseline for ablation study.

A deliberately simple single-agent loop that does navigation AND extraction in
ONE accumulating message context — the opposite of Studeal's parallel
orchestrators + context-isolated extraction sub-agents.

The baseline is intentionally GOOD (not a strawman): it gets the same action
space, the same element-id convention, and a competent system prompt. The only
architectural difference is the accumulating context and the absence of
parallelism.

This module is a LIBRARY (no __main__, no test_ functions). run_ablation.py
drives it as a script.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin

from dealbot.agents.explorer import _do_click, _do_type, _settled_snapshot
from dealbot.agents.marketplace_router import MarketplaceSearchTarget
from dealbot.agents.perception import truncate_snapshot_text
from dealbot.agents.workers.extractor import Offer
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext
from dealbot.scrapers.browser_session import BrowserSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

@dataclass
class NaiveResult:
    offers: list[Offer] = field(default_factory=list)
    turns_used: int = 0
    stop_reason: str = "done"   # "done" | "max_turns" | "error"
    wall_clock_s: float = 0.0


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

NAIVE_REACT_SYSTEM = """You are a deal-hunting browser agent. Your job is to
navigate marketplace search pages, paginate through results, and record every
listing that matches the given spec via record_offer. You handle navigation AND
extraction in a single loop — no separate workers.

The page snapshot shows interactive elements in [id] brackets, e.g.:
  [43]<a href="..." /> "Next"
  [17]<input type="text" /> "Search Kijiji"
Act on elements by their id. Ids change every turn — always use ids from the
CURRENT snapshot.

Tools (emit ONE per turn as JSON):
  {"action":"navigate","url":"https://..."}
      Go directly to a URL.
  {"action":"click","id":43}
      Click element 43. Use for pagination controls, consent banners, etc.
  {"action":"type","id":17,"text":"<query>"}
      Click element 17, type the text, press Enter. Use on the search box.
  {"action":"scroll"}
      Scroll down one viewport — reveals below-fold results.
  {"action":"record_offer","title":"...","price":123.45,"url":"https://...","condition":"used"}
      Record a listing you can see in the snapshot. price must be a number.
      condition must be one of: new, refurbished, used, unknown.
      url may be relative — it will be resolved against the current page URL.
      Record each listing ONCE. Do not revisit listings already recorded.
  {"action":"next_site"}
      Advance to the next marketplace. Use when you have exhausted the current
      site's results (paginated fully or hit a captcha / auth wall).
  {"action":"done","reason":"..."}
      Finish the entire run. Use when all sites have been visited.

Standard flow:
  1. You land on the first marketplace's search page.
  2. Search for the query, paginate, and record every matching listing via
     record_offer. Record listings as you find them — do not wait until the
     end of a site.
  3. When the current site is exhausted, call next_site to advance.
  4. Repeat on every marketplace in order.
  5. After the last marketplace, call done."""


# ---------------------------------------------------------------------------
# Context trim guard (100k chars total, drop oldest non-system messages)
# ---------------------------------------------------------------------------

_CONTEXT_CHAR_LIMIT: int = 100_000


def _total_chars(messages: list[dict]) -> int:
    total = 0
    for m in messages:
        content = m.get("content", "")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            for block in content:
                total += len(block.get("text", ""))
    return total


def _trim_to_limit(messages: list[dict]) -> list[dict]:
    """Drop oldest non-system messages until total chars < _CONTEXT_CHAR_LIMIT."""
    while _total_chars(messages) > _CONTEXT_CHAR_LIMIT and len(messages) > 1:
        # Find first non-system message (index 0 is always the system prompt)
        for i, m in enumerate(messages):
            if m.get("role") != "system":
                messages.pop(i)
                break
        else:
            break
    return messages


# ---------------------------------------------------------------------------
# NaiveReActRunner
# ---------------------------------------------------------------------------

class NaiveReActRunner:
    MAX_TURNS: int = 40

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def run(
        self,
        spec: WatchlistContext,
        targets: list[MarketplaceSearchTarget],
        session: BrowserSession,
    ) -> NaiveResult:
        """Single ReAct loop across all targets, accumulating context."""
        t_start = time.monotonic()
        offers: list[Offer] = []
        target_index = 0

        if not targets:
            return NaiveResult(
                offers=[], turns_used=0, stop_reason="done",
                wall_clock_s=time.monotonic() - t_start,
            )

        # Navigate to the first target
        current_target = targets[target_index]
        try:
            await session.page.goto(
                current_target.entry_url,
                wait_until="domcontentloaded",
                timeout=20_000,
            )
        except Exception as exc:
            logger.warning("NaiveReAct: initial goto failed: %s", exc)
            return NaiveResult(
                offers=[], turns_used=0, stop_reason="error",
                wall_clock_s=time.monotonic() - t_start,
            )

        # Build initial system + user message
        messages: list[dict] = [
            {"role": "system", "content": NAIVE_REACT_SYSTEM},
            {
                "role": "user",
                "content": _initial_prompt(spec, targets),
            },
        ]

        for turn in range(self.MAX_TURNS):
            # Snapshot current page state
            try:
                snap = await _settled_snapshot(session.page)
            except Exception as exc:
                logger.warning("NaiveReAct: snapshot failed on turn %d: %s", turn, exc)
                return NaiveResult(
                    offers=offers, turns_used=turn, stop_reason="error",
                    wall_clock_s=time.monotonic() - t_start,
                )

            current_url = snap.url or ""

            # Append per-turn user message (snapshot + counters)
            turn_text = _turn_prompt(
                snap=snap,
                turn=turn,
                max_turns=self.MAX_TURNS,
                offer_count=len(offers),
                marketplace=current_target.marketplace,
            )
            messages.append({"role": "user", "content": turn_text})

            # Trim if context too large (drop oldest non-system messages)
            messages = _trim_to_limit(messages)

            # LLM call
            try:
                response = await self.llm.complete(
                    messages, response_format={"type": "json_object"},
                )
            except Exception as exc:
                logger.warning("NaiveReAct: LLM call failed on turn %d: %s", turn, exc)
                return NaiveResult(
                    offers=offers, turns_used=turn, stop_reason="error",
                    wall_clock_s=time.monotonic() - t_start,
                )

            messages.append({"role": "assistant", "content": response.content})

            # Parse action
            action, err = _parse_action(response.content)
            if err is not None:
                messages.append({
                    "role": "user",
                    "content": f"Invalid action JSON: {err}. Re-emit one of the listed actions.",
                })
                continue

            action_type = action.get("action", "")

            # --- dispatch actions ---

            if action_type == "done":
                return NaiveResult(
                    offers=offers,
                    turns_used=turn + 1,
                    stop_reason="done",
                    wall_clock_s=time.monotonic() - t_start,
                )

            if action_type == "next_site":
                target_index += 1
                if target_index >= len(targets):
                    # All sites visited
                    return NaiveResult(
                        offers=offers,
                        turns_used=turn + 1,
                        stop_reason="done",
                        wall_clock_s=time.monotonic() - t_start,
                    )
                current_target = targets[target_index]
                try:
                    await session.page.goto(
                        current_target.entry_url,
                        wait_until="domcontentloaded",
                        timeout=20_000,
                    )
                    result_msg = f"Advanced to {current_target.marketplace} at {current_target.entry_url}."
                except Exception as exc:
                    result_msg = f"Failed to navigate to next site: {type(exc).__name__}."
                messages.append({"role": "user", "content": result_msg})
                continue

            if action_type == "record_offer":
                offer, tool_msg = _build_offer(action, current_url, current_target.marketplace)
                if offer is not None:
                    offers.append(offer)
                messages.append({"role": "user", "content": tool_msg})
                continue

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
                url = action.get("url", "")
                if not url:
                    messages.append({
                        "role": "user",
                        "content": "navigate requires 'url' field. Re-emit.",
                    })
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
                elem_id = action.get("id")
                if elem_id is None:
                    messages.append({
                        "role": "user",
                        "content": "click requires 'id' (an [id] from the snapshot). Re-emit.",
                    })
                    continue
                try:
                    result_msg, _ = await _do_click(
                        session.page, session, snap, int(elem_id),
                    )
                except Exception as exc:
                    result_msg = f"click failed: {type(exc).__name__}"
                messages.append({"role": "user", "content": result_msg})
                continue

            if action_type == "type":
                elem_id = action.get("id")
                text = action.get("text")
                if elem_id is None or text is None:
                    messages.append({
                        "role": "user",
                        "content": "type requires 'id' and 'text'. Re-emit.",
                    })
                    continue
                try:
                    result_msg, _ = await _do_type(
                        session.page, session, snap, int(elem_id), str(text),
                    )
                except Exception as exc:
                    result_msg = f"type failed: {type(exc).__name__}"
                messages.append({"role": "user", "content": result_msg})
                continue

            # Unknown action
            messages.append({
                "role": "user",
                "content": (
                    f"Action {action_type!r} is unknown. Valid actions: "
                    "navigate, click, type, scroll, record_offer, next_site, done."
                ),
            })

        # Turn budget exhausted
        return NaiveResult(
            offers=offers,
            turns_used=self.MAX_TURNS,
            stop_reason="max_turns",
            wall_clock_s=time.monotonic() - t_start,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_action(content: str | None) -> tuple[dict | None, str | None]:
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
    return data, None


def _build_offer(
    action: dict,
    current_url: str,
    marketplace: str,
) -> tuple[Offer | None, str]:
    """Parse a record_offer action dict into an Offer, or return an error message."""
    title = action.get("title")
    price_raw = action.get("price")
    url_raw = action.get("url")
    condition = action.get("condition", "unknown")

    # Validate required fields
    if not title or not isinstance(title, str) or not title.strip():
        return None, "invalid offer: missing or empty 'title'"
    if price_raw is None:
        return None, "invalid offer: missing 'price'"
    try:
        price = float(price_raw)
    except (TypeError, ValueError):
        return None, f"invalid offer: 'price' must be a number, got {price_raw!r}"
    if price <= 0:
        return None, f"invalid offer: 'price' must be positive, got {price}"
    if not url_raw or not isinstance(url_raw, str) or not url_raw.strip():
        return None, "invalid offer: missing or empty 'url'"

    # Resolve relative URLs against current page URL
    resolved_url = urljoin(current_url, url_raw.strip()) if current_url else url_raw.strip()

    # Normalize condition
    valid_conditions = {"new", "refurbished", "used", "unknown"}
    if condition not in valid_conditions:
        condition = "unknown"

    try:
        offer = Offer(
            title=title.strip(),
            price=price,
            currency="CAD",
            url=resolved_url,
            condition=condition,  # type: ignore[arg-type]
            marketplace=marketplace,
        )
    except Exception as exc:
        return None, f"invalid offer: {exc}"

    return offer, f"Recorded offer: {title.strip()!r} at ${price:.2f}."


def _initial_prompt(spec: WatchlistContext, targets: list[MarketplaceSearchTarget]) -> str:
    sites_block = "\n".join(
        f"  {i + 1}. {t.marketplace}: {t.entry_url}"
        for i, t in enumerate(targets)
    )
    return (
        f"Watchlist spec:\n{spec.model_dump_json(indent=2)}\n\n"
        f"Marketplaces to visit (in order):\n{sites_block}\n\n"
        "You are on the first marketplace's search page. Start searching, "
        "paginating, and recording offers. Use record_offer for every matching "
        "listing. When finished with a site, call next_site. When all sites are "
        "done, call done."
    )


def _turn_prompt(
    snap: object,
    turn: int,
    max_turns: int,
    offer_count: int,
    marketplace: str,
) -> str:
    text = truncate_snapshot_text(getattr(snap, "text", ""))
    url = getattr(snap, "url", "")
    return (
        f"Turn {turn + 1}/{max_turns} | Site: {marketplace} | "
        f"Offers recorded so far: {offer_count}\n"
        f"Current URL: {url}\n\n"
        f"Page snapshot:\n{text}\n\n"
        "Emit the next action as JSON."
    )
