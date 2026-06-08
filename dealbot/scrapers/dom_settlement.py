"""Event-driven DOM settlement for browser sessions.

The pattern: a `DomSettlementWatchdog` is set up ONCE per session at
`BrowserSession.__aenter__` time and subscribes to CDP events:

  - `DOM.documentUpdated`: every mutation. Triggers a debounce timer; the
    "quiet" event fires when the debounce expires without another mutation.
    Tools that mutate the page (click/type/scroll/navigate) call
    `wait_for_settlement()` after the action and block until the next quiet
    window. This replaces `asyncio.sleep` heuristics that race with the SPA.

  - `Page.frameNavigated` + `Page.loadEventFired`: navigation lifecycle.
    Tracked but not currently surfaced — used as additional signal for
    future tuning.

  - `Page.javascriptDialogOpening`: cookie banners, age gates, alert(...)
    boxes. Handler auto-dismisses known categories (accept consent dialogs,
    dismiss everything else) to unblock the page without escalation.

  - `Network.responseReceived` + `Network.getResponseBody`: passively
    inspects every response URL against a price-API pattern set. Matching
    responses get their JSON body captured into the session-scoped
    `intercepted_responses` list. OfferExtractor reads that list directly
    — gets us rendered prices pre-DOM, often more reliable than scraping.

The watchdog is composed onto BrowserSession via the
`browser_session.py` module so production (Browserbase) and eval
(LocalPlaywright) both get the same settlement guarantees.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from playwright.async_api import Page

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Price-API URL patterns. Matches against response URLs to decide whether to
# capture the body. Conservative on purpose — too many matches = wasted work
# and bigger captured-responses list.
# ---------------------------------------------------------------------------
_PRICE_API_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"/api/(product|catalog|item|offer|price|listing)s?\b", re.IGNORECASE),
    re.compile(r"/graphql.*\b(product|price|offer|stock)\b", re.IGNORECASE),
    re.compile(r"/v\d+/(product|offer|price)", re.IGNORECASE),
    re.compile(r"\b(prices?|offers?)\.(json|xml)", re.IGNORECASE),
)


# ---------------------------------------------------------------------------
# Dialog auto-dismiss policy
# ---------------------------------------------------------------------------
_CONSENT_KEYWORDS = (
    "cookie", "consent", "gdpr", "privacy", "tracking",
    "accept", "agree", "essential", "preferences",
)
_AGE_GATE_KEYWORDS = ("age", "18", "21", "adult", "verify your")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class InterceptedResponse:
    """A response captured because its URL matched a price-API pattern."""

    url: str
    body: str
    status: int = 200
    content_type: str = ""
    captured_at: float = 0.0  # time.monotonic() at capture


@dataclass
class DialogEncounter:
    """Logged whenever a JS dialog opens. Surfaces to PageReader for findings."""

    dialog_type: str               # "alert" | "confirm" | "prompt" | "beforeunload"
    message: str
    auto_action: str               # "accept_consent" | "dismiss_age" | "dismiss_other"


# ---------------------------------------------------------------------------
# The watchdog
# ---------------------------------------------------------------------------

class DomSettlementWatchdog:
    """Per-session CDP event subscriber.

    Construct with a Playwright `Page` + a list to append intercepted
    responses to. Call `start()` once after the page is available (typically
    in session `__aenter__`). Call `stop()` to detach (in `__aexit__`).
    """

    def __init__(
        self,
        page: Page,
        intercepted_responses: list[InterceptedResponse],
        dialog_log: list[DialogEncounter] | None = None,
    ) -> None:
        self._page = page
        self._intercepted = intercepted_responses
        self._dialog_log: list[DialogEncounter] = dialog_log if dialog_log is not None else []
        self._cdp = None
        self._started = False

        # Debounce state for DOM settlement.
        self._dom_quiet_event = asyncio.Event()
        self._dom_quiet_event.set()  # start quiet (no pending mutations)
        self._quiet_task: asyncio.Task | None = None
        self._debounce_ms = 300

        # Navigation state — not currently consumed, kept for future tuning.
        self._load_event_fired = True
        self._navigation_pending = False

    # ---- lifecycle ----

    async def start(self, *, debounce_ms: int = 300) -> None:
        """Attach CDP and subscribe to events. Idempotent."""
        if self._started:
            return
        self._debounce_ms = debounce_ms

        self._cdp = await self._page.context.new_cdp_session(self._page)
        # Enable the domains we'll subscribe to.
        await asyncio.gather(
            self._cdp.send("DOM.enable"),
            self._cdp.send("Page.enable"),
            self._cdp.send("Network.enable"),
        )

        # Sync handlers — these are called by Playwright's CDP event loop.
        # Anything async happens via asyncio.create_task inside the handler.
        self._cdp.on("DOM.documentUpdated", self._on_dom_updated)
        self._cdp.on("Page.frameNavigated", self._on_frame_navigated)
        self._cdp.on("Page.loadEventFired", self._on_load_event)
        self._cdp.on("Page.javascriptDialogOpening", self._on_dialog_opening)
        self._cdp.on("Network.responseReceived", self._on_response_received)

        self._started = True

    async def stop(self) -> None:
        """Detach CDP. Safe to call multiple times."""
        if not self._started:
            return
        self._started = False
        if self._quiet_task and not self._quiet_task.done():
            self._quiet_task.cancel()
        if self._cdp:
            try:
                await self._cdp.detach()
            except Exception:
                pass
            self._cdp = None

    # ---- public surface for tools ----

    async def wait_for_settlement(
        self,
        after_action: str = "",
        timeout_ms: int = 5000,
        debounce_ms: int = 300,
    ) -> None:
        """Block until DOM has been quiet for `debounce_ms` ms (or timeout).

        Replaces fixed `asyncio.sleep` after mutations. If mutations are
        still firing when `timeout_ms` elapses, we return anyway and let
        the caller proceed — the next snapshot may be slightly stale but
        we don't deadlock.
        """
        # Allow per-call override.
        self._debounce_ms = debounce_ms

        timeout_s = timeout_ms / 1000.0
        try:
            await asyncio.wait_for(self._dom_quiet_event.wait(), timeout=timeout_s)
        except asyncio.TimeoutError:
            logger.debug(
                "watchdog: settlement timeout after %dms (action=%r)",
                timeout_ms, after_action,
            )

    # ---- event handlers ----

    def _on_dom_updated(self, params: dict[str, Any]) -> None:
        """A DOM mutation occurred — restart the debounce window."""
        self._dom_quiet_event.clear()
        if self._quiet_task and not self._quiet_task.done():
            self._quiet_task.cancel()
        self._quiet_task = asyncio.create_task(self._mark_quiet_after_debounce())

    async def _mark_quiet_after_debounce(self) -> None:
        try:
            await asyncio.sleep(self._debounce_ms / 1000.0)
            self._dom_quiet_event.set()
        except asyncio.CancelledError:
            # Another mutation arrived before debounce expired — that's fine.
            pass

    def _on_frame_navigated(self, params: dict[str, Any]) -> None:
        frame = params.get("frame", {})
        # Only track top-level navigation; subframes are noisy.
        if frame.get("parentId"):
            return
        self._navigation_pending = True
        self._load_event_fired = False

    def _on_load_event(self, params: dict[str, Any]) -> None:
        self._load_event_fired = True
        self._navigation_pending = False

    def _on_dialog_opening(self, params: dict[str, Any]) -> None:
        """Auto-dismiss known dialog types; log everything."""
        if self._cdp is None:
            return
        asyncio.create_task(self._handle_dialog(params))

    async def _handle_dialog(self, params: dict[str, Any]) -> None:
        dialog_type = params.get("type", "alert")
        message = params.get("message", "")
        message_lower = message.lower()

        accept: bool
        auto_action: str
        if any(kw in message_lower for kw in _CONSENT_KEYWORDS):
            accept = True
            auto_action = "accept_consent"
        elif any(kw in message_lower for kw in _AGE_GATE_KEYWORDS):
            accept = True
            auto_action = "dismiss_age"
        else:
            accept = False
            auto_action = "dismiss_other"
            logger.info(
                "watchdog: unexpected dialog (%s): %s", dialog_type, message[:120],
            )

        self._dialog_log.append(DialogEncounter(
            dialog_type=dialog_type,
            message=message,
            auto_action=auto_action,
        ))

        if self._cdp is None:
            return
        try:
            await self._cdp.send(
                "Page.handleJavaScriptDialog", {"accept": accept},
            )
        except Exception as exc:
            logger.debug("watchdog: dialog handle failed: %s", exc)

    def _on_response_received(self, params: dict[str, Any]) -> None:
        """Capture body for responses whose URL matches price-API patterns."""
        if self._cdp is None:
            return
        response = params.get("response", {})
        url = response.get("url", "")
        if not _matches_price_api(url):
            return
        request_id = params.get("requestId")
        if not request_id:
            return
        asyncio.create_task(self._capture_response(request_id, url, response))

    async def _capture_response(
        self, request_id: str, url: str, response: dict[str, Any],
    ) -> None:
        if self._cdp is None:
            return
        try:
            body_data = await self._cdp.send(
                "Network.getResponseBody", {"requestId": request_id},
            )
        except Exception as exc:
            logger.debug("watchdog: getResponseBody failed for %s: %s", url[:80], exc)
            return

        body = body_data.get("body", "")
        if body_data.get("base64Encoded"):
            # Skip binary captures — we only care about JSON/text.
            return

        import time
        self._intercepted.append(InterceptedResponse(
            url=url,
            body=body,
            status=response.get("status", 0),
            content_type=response.get("mimeType", ""),
            captured_at=time.monotonic(),
        ))
        logger.debug(
            "watchdog: captured price-API response %s (%d bytes)", url[:80], len(body),
        )

    # ---- introspection (for tests) ----

    @property
    def is_dom_quiet(self) -> bool:
        return self._dom_quiet_event.is_set()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _matches_price_api(url: str) -> bool:
    for pat in _PRICE_API_PATTERNS:
        if pat.search(url):
            return True
    return False
