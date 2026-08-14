"""Browser session abstractions.

The agent doesn't care whether it's driving a remote Browserbase session
(production) or a local headless Chromium (evals / dev). It just wants an
async context manager that exposes a Playwright `Page`, a settlement
watchdog, and a captured-responses list.

This module owns:
  - `BrowserSession` — the ABC.
  - `BrowserbaseSession` — production: remote Browserbase + proxy rotation,
    bounded by a process-wide semaphore (BROWSERBASE_MAX_SESSIONS).
  - `LocalPlaywrightSession` — eval/dev: local Playwright, no remote API,
    no proxies, no rate limits. Used by integration tests against fixture
    HTML pages so we don't burn Browserbase credits on every CI run.
  - `build_browser_session()` — composition root. Picks an impl from
    `AGENT_BROWSER_BACKEND` env var (or explicit arg).

The settlement watchdog and interception types are stubbed here for v1.
Phase 1.2c replaces the no-op `DomSettlementWatchdog` with the real
CDP-event-driven implementation from `dom_settlement.py`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from abc import ABC, abstractmethod

from playwright.async_api import BrowserContext, Page, async_playwright

from dealbot.scrapers.agentcore_session import (
    close_browser as _ac_close_browser,
    get_session_sem as _ac_get_session_sem,
    open_browser as _ac_open_browser,
)
from dealbot.scrapers.browserbase_session import (
    BROWSERBASE_MAX_SESSIONS,
    create_session as _bb_create_session,
    get_session_sem as _bb_get_session_sem,
    terminate_session as _bb_terminate_session,
)
from dealbot.scrapers.dom_settlement import (
    DomSettlementWatchdog,
    InterceptedResponse,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Media blocking (shared across subclasses)
#
# Images dominate metered bandwidth: the 2026-08 Browserbase cycle measured
# ~100 MB per browser-hour with images loading (9.4 GB over ~94 h), at
# residential-proxy rates of $12/GB. Aborting image/media/font requests cuts
# that multiple-fold. CSS and JS stay: layout drives perception's bboxes and
# SPAs render listings via JS. Thumbnails survive because capture falls back
# to the img src attribute when naturalWidth is 0 — the same never-loaded
# path below-fold cards already take. AGENT_BLOCK_MEDIA=0 is the rollback.
# ---------------------------------------------------------------------------
_BLOCKED_RESOURCE_TYPES: frozenset[str] = frozenset({"image", "media", "font"})


def media_blocking_enabled() -> bool:
    return os.environ.get("AGENT_BLOCK_MEDIA", "1").lower() not in ("0", "false", "no")


def should_abort_request(resource_type: str) -> bool:
    return resource_type in _BLOCKED_RESOURCE_TYPES


# ---------------------------------------------------------------------------
# The ABC
# ---------------------------------------------------------------------------

class BrowserSession(ABC):
    """Abstract base. Subclasses provide a live Playwright `Page` on entry
    and clean up on exit. Both production and local impls share this surface
    so the orchestrator can be wired against either via DI.
    """

    page: Page
    watchdog: DomSettlementWatchdog
    intercepted_responses: list[InterceptedResponse]

    @abstractmethod
    async def __aenter__(self) -> "BrowserSession":
        ...

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        ...

    async def _apply_media_blocking(self, ctx: BrowserContext) -> None:
        """Abort image/media/font requests context-wide (covers popups too).

        Best-effort: a routing failure degrades to unblocked bandwidth,
        never a dead session.
        """
        if not media_blocking_enabled():
            return

        async def _route(route) -> None:
            try:
                if should_abort_request(route.request.resource_type):
                    await route.abort()
                else:
                    await route.continue_()
            except Exception:
                pass  # page may be mid-navigation; the request is already moot

        try:
            await ctx.route("**/*", _route)
        except Exception as exc:
            logger.warning("media blocking not applied: %s", exc)

    # -----------------------------------------------------------------
    # Popup / new-tab handling (shared across subclasses)
    #
    # Sites spawn tabs via target="_blank" or window.open(). Without a
    # handler, those tabs orphan and the main page freezes (v13 spike bug).
    # Subclasses call `_wire_popup_handler(ctx)` once, after creating the
    # BrowserContext but before returning from __aenter__.
    # -----------------------------------------------------------------

    def _wire_popup_handler(self, ctx: BrowserContext) -> None:
        """Register `_on_new_page` for the context's `page` event.

        Fired every time a new tab/popup spawns in this context. The handler
        decides whether to promote the new page to `self.page` or discard it.
        """
        ctx.on("page", self._on_new_page)

    async def _on_new_page(self, new_page: Page) -> None:
        """Promote new-tab-with-real-URL to `self.page`; close blank popups.

        - Waits up to 5s for the popup to navigate away from about:blank
          (Chrome creates tabs at about:blank and navigates asynchronously).
        - If URL is still 'about:blank'/empty after the wait: close the popup;
          `self.page` unchanged.
        - Otherwise: wait for DOM ready, stop the old watchdog, swap
          `self.page = new_page`, start a fresh watchdog on the new page,
          close the old page.
        """
        try:
            # Short window — real navigations from window.open finish in
            # <100ms. Waiting longer just delays closing trash popups.
            await new_page.wait_for_url(
                lambda u: u not in ("about:blank", ""),
                timeout=1000,
            )
        except Exception:
            # Timed out — popup stayed on about:blank. Treat as trash.
            pass

        if new_page.url in ("about:blank", ""):
            try:
                await new_page.close()
            except Exception:
                pass
            return

        # Have a real URL. Wait for DOM to be usable before wiring watchdog.
        try:
            await new_page.wait_for_load_state("domcontentloaded", timeout=5000)
        except Exception:
            pass

        old_page = self.page
        try:
            await self.watchdog.stop()
        except Exception:
            pass
        self.page = new_page
        self.watchdog = DomSettlementWatchdog(new_page, self.intercepted_responses)
        try:
            await self.watchdog.start()
        except Exception as exc:
            logger.warning("popup handler: new watchdog start failed: %s", exc)
        try:
            await old_page.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Local Playwright (evals, dev)
# ---------------------------------------------------------------------------

class LocalPlaywrightSession(BrowserSession):
    """Headless local Playwright session for evals and local dev.

    No remote API calls, no semaphore, no proxy rotation. Just a local
    Chromium instance launched directly. Cheap, fast, and good enough for
    integration tests against saved fixture HTML files.

    Optional `storage_state` loads a previously-saved cookie + localStorage
    snapshot (Playwright's standard auth-persistence format). Used for sites
    that require a logged-in session (e.g., FB Marketplace) — run the auth
    helper once to produce the state file, then sessions can reuse it for
    days/weeks until the cookies expire.
    """

    def __init__(
        self,
        *,
        headless: bool = True,
        storage_state: str | None = None,
    ) -> None:
        self._headless = headless
        self._storage_state = storage_state
        self._pw_context = None
        self._browser = None
        self.intercepted_responses = []

    async def __aenter__(self) -> "LocalPlaywrightSession":
        self._pw_context = async_playwright()
        pw = await self._pw_context.__aenter__()
        # STUDEAL_HEADED=1 overrides constructor default — lets you watch
        # the browser drive in real time without touching composition.
        headless = self._headless
        if os.environ.get("STUDEAL_HEADED") == "1":
            headless = False
        self._browser = await pw.chromium.launch(headless=headless)
        ctx_kwargs = {}
        if self._storage_state:
            from pathlib import Path
            if Path(self._storage_state).exists():
                ctx_kwargs["storage_state"] = self._storage_state
            else:
                logger.warning(
                    "LocalPlaywrightSession: storage_state %r not found; "
                    "starting fresh context", self._storage_state,
                )
        ctx = await self._browser.new_context(**ctx_kwargs)
        await self._apply_media_blocking(ctx)
        self.page = await ctx.new_page()
        # Wire popup handler AFTER the initial page is created — Playwright's
        # "page" event fires for every new_page(), including this first one.
        # Registering after means only genuine popups (target=_blank, window.open)
        # invoke _on_new_page, not the initial page bootstrap.
        self._wire_popup_handler(ctx)
        # Watchdog needs a live Page, so construct + start after the page exists.
        self.watchdog = DomSettlementWatchdog(self.page, self.intercepted_responses)
        try:
            await self.watchdog.start()
        except Exception as exc:
            logger.warning("LocalPlaywrightSession: watchdog start failed: %s", exc)
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self.watchdog is not None:
            try:
                await self.watchdog.stop()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw_context:
            try:
                await self._pw_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._pw_context = None


# ---------------------------------------------------------------------------
# Browserbase (production)
# ---------------------------------------------------------------------------

class BrowserbaseSession(BrowserSession):
    """Production: remote Browserbase session via CDP with proxy rotation.

    Acquires a slot from the module-level semaphore (capped at
    BROWSERBASE_MAX_SESSIONS) before opening the remote session, so we
    never exceed the plan's concurrent-session limit. Cleans up the remote
    session on exit even if Playwright tear-down fails.
    """

    def __init__(self, *, proxies: bool = True) -> None:
        self._api_key = os.environ.get("BROWSERBASE_API_KEY", "")
        self._project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
        self._proxies = proxies
        self._sem: asyncio.Semaphore | None = None
        self._sem_acquired = False
        self._session_id: str | None = None
        self._pw_context = None
        self._browser = None
        self.intercepted_responses = []

    async def __aenter__(self) -> "BrowserbaseSession":
        if not self._api_key or not self._project_id:
            raise RuntimeError("BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID not set")

        self._sem = _bb_get_session_sem()
        await self._sem.acquire()
        self._sem_acquired = True

        try:
            self._session_id, connect_url = await _bb_create_session(
                self._api_key, self._project_id, proxies=self._proxies,
            )
            self._pw_context = async_playwright()
            pw = await self._pw_context.__aenter__()
            self._browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx: BrowserContext = self._browser.contexts[0]
            await self._apply_media_blocking(ctx)
            self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # Wire popup handler AFTER the initial page is bound — see
            # LocalPlaywrightSession for rationale (avoid handler firing on
            # the initial context page).
            self._wire_popup_handler(ctx)
            self.watchdog = DomSettlementWatchdog(self.page, self.intercepted_responses)
            try:
                await self.watchdog.start()
            except Exception as exc:
                logger.warning("BrowserbaseSession: watchdog start failed: %s", exc)
            return self
        except Exception:
            await self._cleanup()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            try:
                await watchdog.stop()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw_context:
            try:
                await self._pw_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._pw_context = None
        if self._session_id:
            await _bb_terminate_session(self._api_key, self._session_id)
            self._session_id = None
        if self._sem_acquired and self._sem is not None:
            self._sem.release()
            self._sem_acquired = False


# ---------------------------------------------------------------------------
# AgentCore (production, credits-billed)
# ---------------------------------------------------------------------------

class AgentCoreBrowserSession(BrowserSession):
    """Amazon Bedrock AgentCore managed browser via CDP.

    Same lifecycle shape as BrowserbaseSession: semaphore slot first, remote
    CDP connect, cleanup that always releases the remote session and the
    slot. Sessions exit from AWS IPs — no residential reputation, no
    Browserbase stealth fingerprint; see agentcore_session.py for what that
    trades away and why.
    """

    def __init__(self) -> None:
        self._client = None
        self._sem: asyncio.Semaphore | None = None
        self._sem_acquired = False
        self._pw_context = None
        self._browser = None
        self.intercepted_responses = []

    async def __aenter__(self) -> "AgentCoreBrowserSession":
        self._sem = _ac_get_session_sem()
        await self._sem.acquire()
        self._sem_acquired = True

        try:
            self._client, ws_url, headers = await _ac_open_browser()
            self._pw_context = async_playwright()
            pw = await self._pw_context.__aenter__()
            self._browser = await pw.chromium.connect_over_cdp(ws_url, headers=headers)
            ctx: BrowserContext = (
                self._browser.contexts[0] if self._browser.contexts
                else await self._browser.new_context()
            )
            await self._apply_media_blocking(ctx)
            self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            # Wire popup handler AFTER the initial page is bound — see
            # LocalPlaywrightSession for rationale.
            self._wire_popup_handler(ctx)
            self.watchdog = DomSettlementWatchdog(self.page, self.intercepted_responses)
            try:
                await self.watchdog.start()
            except Exception as exc:
                logger.warning("AgentCoreBrowserSession: watchdog start failed: %s", exc)
            return self
        except Exception:
            await self._cleanup()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
        watchdog = getattr(self, "watchdog", None)
        if watchdog is not None:
            try:
                await watchdog.stop()
            except Exception:
                pass
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._pw_context:
            try:
                await self._pw_context.__aexit__(None, None, None)
            except Exception:
                pass
            self._pw_context = None
        if self._client is not None:
            await _ac_close_browser(self._client)
            self._client = None
        if self._sem_acquired and self._sem is not None:
            self._sem.release()
            self._sem_acquired = False


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------

def build_browser_session(backend: str | None = None) -> BrowserSession:
    """Return a concrete BrowserSession for `async with`.

    `backend` overrides the env var if provided. Otherwise reads
    `AGENT_BROWSER_BACKEND` (default: "browserbase").
    """
    chosen = (backend or os.environ.get("AGENT_BROWSER_BACKEND", "browserbase")).lower()
    if chosen == "local":
        return LocalPlaywrightSession()
    if chosen == "browserbase":
        return BrowserbaseSession()
    if chosen == "agentcore":
        return AgentCoreBrowserSession()
    raise ValueError(
        f"Unknown AGENT_BROWSER_BACKEND: {chosen!r}. Expected 'browserbase', 'agentcore', or 'local'."
    )
