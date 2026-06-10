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


# ---------------------------------------------------------------------------
# Local Playwright (evals, dev)
# ---------------------------------------------------------------------------

class LocalPlaywrightSession(BrowserSession):
    """Headless local Playwright session for evals and local dev.

    No remote API calls, no semaphore, no proxy rotation. Just a local
    Chromium instance launched directly. Cheap, fast, and good enough for
    integration tests against saved fixture HTML files.
    """

    def __init__(self, *, headless: bool = True) -> None:
        self._headless = headless
        self._pw_context = None
        self._browser = None
        self.intercepted_responses = []

    async def __aenter__(self) -> "LocalPlaywrightSession":
        self._pw_context = async_playwright()
        pw = await self._pw_context.__aenter__()
        self._browser = await pw.chromium.launch(headless=self._headless)
        ctx = await self._browser.new_context()
        self.page = await ctx.new_page()
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
            self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
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
    raise ValueError(
        f"Unknown AGENT_BROWSER_BACKEND: {chosen!r}. Expected 'browserbase' or 'local'."
    )
