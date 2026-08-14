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


def _session_payload(project_id: str, proxies: bool) -> dict:
    """Session config, tuned to the strongest options our plan allows.

    Empirically probed 2026-07-30 against the live API:
      - browserSettings.verified / advancedStealth → 403 (Enterprise-only)
      - browserSettings.os="mac"                   → 400 (verified users only)
      - browserSettings.fingerprint (locales/devices/os/screen) → accepted
      - proxies[].geolocation with city            → accepted

    Geography is load-bearing beyond stealth: Canadian marketplaces serve
    different (or no) inventory to US exit IPs, so proxies pin to CA/Toronto
    and the fingerprint locale matches (en-CA), keeping IP and browser story
    consistent — mismatches are themselves a bot signal.
    """
    country = os.environ.get("BROWSERBASE_PROXY_COUNTRY", "CA")
    city = os.environ.get("BROWSERBASE_PROXY_CITY", "TORONTO")
    width = int(os.environ.get("BROWSERBASE_VIEWPORT_W", "1920"))
    height = int(os.environ.get("BROWSERBASE_VIEWPORT_H", "1080"))

    payload: dict = {
        "projectId": project_id,
        # keepAlive would hold the session open after its owner dies — a
        # killed worker's lanes then squat the concurrency pool for the
        # full timeout (observed 2026-08-11: restart spree ate all 25
        # slots). Lanes reconnect never; let sessions die with the CDP
        # connection and cap stragglers at 15 minutes.
        "keepAlive": False,
        "timeout": int(os.environ.get("BROWSERBASE_SESSION_TIMEOUT_S", "900")),
        "region": os.environ.get("BROWSERBASE_REGION", "us-east-1"),
        "browserSettings": {
            "viewport": {"width": width, "height": height},
            "blockAds": True,
            "solveCaptchas": True,
            "fingerprint": {
                "devices": ["desktop"],
                "locales": [os.environ.get("BROWSERBASE_LOCALE", "en-CA")],
                "operatingSystems": [os.environ.get("BROWSERBASE_FP_OS", "windows")],
                "screen": {
                    "minWidth": width, "maxWidth": width,
                    "minHeight": height, "maxHeight": height,
                },
            },
        },
    }
    # Enterprise-only; opt-in so the API never 403s the whole session for us.
    if os.environ.get("BROWSERBASE_VERIFIED", "").lower() in ("1", "true", "yes"):
        payload["browserSettings"]["verified"] = True
    if proxies:
        geo: dict = {"country": country}
        if city:
            geo["city"] = city
        payload["proxies"] = [{"type": "browserbase", "geolocation": geo}]
    return payload


async def create_session(
    api_key: str, project_id: str, proxies: bool = False,
) -> tuple[str, str]:
    """Returns (session_id, connect_url). Retries on 429 with exponential backoff.

    Guarded by the daily session cap: past DAILY_BROWSER_SESSION_CAP the
    creation fails visibly (lane deadline machinery turns that into an
    honest lane failure) instead of silently burning more budget.
    """
    from dealbot.costs import build_meter

    meter = build_meter()
    if not await meter.session_cap_ok():
        raise RuntimeError("daily browser-session cap reached")
    # Monthly hard cap on the one metered-dollar backend: sized to the plan's
    # included quota so the bill can never exceed the flat price.
    if not await meter.bb_month_cap_ok():
        raise RuntimeError("monthly browserbase session cap reached")
    await meter.record_session()
    await meter.record_bb_session()

    payload = _session_payload(project_id, proxies)
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
