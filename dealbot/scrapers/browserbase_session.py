"""Browserbase HTTP helpers + shared session semaphore.

This module owns the low-level Browserbase REST calls (session create /
terminate, 429 retry) and the process-wide concurrency cap. It does NOT
own the lifecycle abstraction — that lives in `browser_session.py` as
`BrowserbaseSession`, which composes these helpers.

Splitting this way lets us unit-test the HTTP helpers independent of the
lifecycle ABC and share the semaphore across any callers that talk to
Browserbase.
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx

logger = logging.getLogger(__name__)

_BROWSERBASE_API = "https://api.browserbase.com/v1"
BROWSERBASE_MAX_SESSIONS = int(os.environ.get("BROWSERBASE_MAX_SESSIONS", "3"))
_MAX_SESSION_RETRIES = 5

_session_sem: asyncio.Semaphore | None = None


def get_session_sem() -> asyncio.Semaphore:
    """Process-wide semaphore capping concurrent Browserbase sessions.

    Imported by `browser_session.BrowserbaseSession` to gate session opens.
    Lazy because asyncio.Semaphore needs a running event loop.
    """
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
