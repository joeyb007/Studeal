from __future__ import annotations

import asyncio
import logging
import os

import httpx
from playwright.async_api import (
    BrowserContext,
    Page,
    async_playwright,
)

logger = logging.getLogger(__name__)

_BROWSERBASE_API = "https://api.browserbase.com/v1"
BROWSERBASE_MAX_SESSIONS = int(os.environ.get("BROWSERBASE_MAX_SESSIONS", "3"))
_MAX_SESSION_RETRIES = 5

_session_sem: asyncio.Semaphore | None = None


def _get_session_sem() -> asyncio.Semaphore:
    global _session_sem
    if _session_sem is None:
        _session_sem = asyncio.Semaphore(BROWSERBASE_MAX_SESSIONS)
    return _session_sem


async def create_session(
    api_key: str, project_id: str, proxies: bool = False,
) -> tuple[str, str]:
    """Returns (session_id, connect_url). Retries on 429 with exponential backoff."""
    payload: dict = {"projectId": project_id, "keepAlive": True, "timeout": 3600}
    if proxies:
        payload["proxies"] = True
    for attempt in range(_MAX_SESSION_RETRIES):
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(
                f"{_BROWSERBASE_API}/sessions",
                headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
                json=payload,
            )
        if resp.status_code == 429:
            if attempt == _MAX_SESSION_RETRIES - 1:
                resp.raise_for_status()
            retry_after = int(resp.headers.get("retry-after", 2))
            wait = retry_after * (2 ** attempt)
            logger.debug("browserbase: 429, retrying in %ds (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
            continue
        resp.raise_for_status()
        data = resp.json()
        return data["id"], data["connectUrl"]
    resp.raise_for_status()
    return "", ""  # unreachable


async def terminate_session(api_key: str, session_id: str) -> None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"{_BROWSERBASE_API}/sessions/{session_id}",
                headers={"X-BB-API-Key": api_key, "Content-Type": "application/json"},
                json={"status": "REQUEST_RELEASE"},
            )
        logger.debug("browserbase: terminated session %s", session_id)
    except Exception as exc:
        logger.debug("browserbase: failed to terminate session %s: %s", session_id, exc)


class BrowserSession:
    """Async context manager owning a single Browserbase session.

    Acquires a slot from the module-level semaphore (capped at
    BROWSERBASE_MAX_SESSIONS) before opening a Playwright Page over CDP.
    Access the page via `self.page` after `__aenter__`.

    Usage:
        async with BrowserSession(proxies=True) as bs:
            await bs.page.goto("https://amazon.ca")
            ...
    """

    def __init__(self, *, proxies: bool = False) -> None:
        self._api_key = os.environ.get("BROWSERBASE_API_KEY", "")
        self._project_id = os.environ.get("BROWSERBASE_PROJECT_ID", "")
        self._proxies = proxies
        self._sem: asyncio.Semaphore | None = None
        self._sem_acquired = False
        self._session_id: str | None = None
        self._pw_context = None
        self._browser = None
        self.page: Page | None = None

    async def __aenter__(self) -> BrowserSession:
        if not self._api_key or not self._project_id:
            raise RuntimeError("BROWSERBASE_API_KEY or BROWSERBASE_PROJECT_ID not set")

        self._sem = _get_session_sem()
        await self._sem.acquire()
        self._sem_acquired = True

        try:
            self._session_id, connect_url = await create_session(
                self._api_key, self._project_id, proxies=self._proxies,
            )
            self._pw_context = async_playwright()
            pw = await self._pw_context.__aenter__()
            self._browser = await pw.chromium.connect_over_cdp(connect_url)
            ctx: BrowserContext = self._browser.contexts[0]
            self.page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            return self
        except Exception:
            await self._cleanup()
            raise

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        await self._cleanup()

    async def _cleanup(self) -> None:
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
            await terminate_session(self._api_key, self._session_id)
            self._session_id = None
        if self._sem_acquired and self._sem is not None:
            self._sem.release()
            self._sem_acquired = False
