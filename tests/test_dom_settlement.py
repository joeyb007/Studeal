"""Tests for dealbot.scrapers.dom_settlement.

Strategy:
  - Behavioral unit tests use a MockCdp that lets us trigger CDP events
    programmatically (no real Chromium). We assert on the watchdog's
    `wait_for_settlement`, dialog handling, and response interception
    in isolation.
  - One integration test exercises the watchdog against a real
    LocalPlaywrightSession page, verifying settlement triggers from a
    real DOM mutation. Skipped if Playwright Chromium isn't installed.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

import pytest

from dealbot.scrapers.dom_settlement import (
    DialogEncounter,
    DomSettlementWatchdog,
    InterceptedResponse,
    _matches_price_api,
)


# ---------------------------------------------------------------------------
# Mock CDP session
# ---------------------------------------------------------------------------

class MockCdpSession:
    """Stand-in for a Playwright CDP session.

    `send()` records the call and returns a stubbed result.
    `on()` registers a handler. We can fire events into those handlers
    via `trigger(method, params)`.
    """

    def __init__(self) -> None:
        self.sent_commands: list[tuple[str, dict | None]] = []
        self._handlers: dict[str, list[Callable]] = {}
        self.detached = False
        # Optional canned response for getResponseBody
        self.response_body_for_request: dict[str, dict] = {}

    async def send(self, method: str, params: dict | None = None) -> Any:
        self.sent_commands.append((method, params))
        if method == "Network.getResponseBody":
            req_id = (params or {}).get("requestId", "")
            return self.response_body_for_request.get(req_id, {"body": "", "base64Encoded": False})
        return {}

    def on(self, method: str, handler: Callable) -> None:
        self._handlers.setdefault(method, []).append(handler)

    def trigger(self, method: str, params: dict) -> None:
        """Fire a synthetic CDP event into registered handlers."""
        for h in self._handlers.get(method, []):
            h(params)

    async def detach(self) -> None:
        self.detached = True


class MockPageContext:
    def __init__(self, cdp: MockCdpSession) -> None:
        self._cdp = cdp

    async def new_cdp_session(self, page: Any) -> MockCdpSession:
        return self._cdp


class MockPage:
    def __init__(self, cdp: MockCdpSession) -> None:
        self.context = MockPageContext(cdp)


def _new_watchdog() -> tuple[DomSettlementWatchdog, MockCdpSession, list[InterceptedResponse], list[DialogEncounter]]:
    cdp = MockCdpSession()
    page = MockPage(cdp)
    intercepted: list[InterceptedResponse] = []
    dialogs: list[DialogEncounter] = []
    wd = DomSettlementWatchdog(page, intercepted, dialogs)
    return wd, cdp, intercepted, dialogs


# ---------------------------------------------------------------------------
# Pure functions
# ---------------------------------------------------------------------------

def test_matches_price_api_positive_cases():
    assert _matches_price_api("https://amazon.ca/api/product/B0XYZ")
    assert _matches_price_api("https://bestbuy.ca/api/products")
    assert _matches_price_api("https://walmart.ca/graphql?query=product")
    assert _matches_price_api("https://apple.com/v2/offers/123")
    assert _matches_price_api("https://example.com/prices.json")


def test_matches_price_api_negative_cases():
    assert not _matches_price_api("https://amazon.ca/static/main.js")
    assert not _matches_price_api("https://amazon.ca/images/logo.png")
    assert not _matches_price_api("https://example.com/")
    assert not _matches_price_api("https://example.com/api/users")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_enables_required_cdp_domains_and_subscribes():
    wd, cdp, _, _ = _new_watchdog()

    await wd.start()

    sent = [m for m, _ in cdp.sent_commands]
    assert "DOM.enable" in sent
    assert "Page.enable" in sent
    assert "Network.enable" in sent
    assert "DOM.documentUpdated" in wd._page.context._cdp._handlers
    assert "Page.javascriptDialogOpening" in cdp._handlers
    assert "Network.responseReceived" in cdp._handlers


@pytest.mark.asyncio
async def test_start_idempotent():
    wd, cdp, _, _ = _new_watchdog()
    await wd.start()
    first_count = len(cdp.sent_commands)
    await wd.start()
    assert len(cdp.sent_commands) == first_count   # no duplicate enables


@pytest.mark.asyncio
async def test_stop_detaches_cdp():
    wd, cdp, _, _ = _new_watchdog()
    await wd.start()
    await wd.stop()
    assert cdp.detached


@pytest.mark.asyncio
async def test_stop_safe_when_never_started():
    wd, cdp, _, _ = _new_watchdog()
    await wd.stop()   # should not raise
    assert not cdp.detached


# ---------------------------------------------------------------------------
# wait_for_settlement
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_wait_returns_immediately_when_already_quiet():
    wd, _, _, _ = _new_watchdog()
    await wd.start()

    # No DOM events have fired → already quiet
    start = asyncio.get_event_loop().time()
    await wd.wait_for_settlement(timeout_ms=2000, debounce_ms=300)
    elapsed = asyncio.get_event_loop().time() - start
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_wait_blocks_then_returns_after_dom_quiet():
    """A DOM mutation fires; wait_for_settlement returns ~debounce_ms later."""
    wd, cdp, _, _ = _new_watchdog()
    await wd.start()

    # Simulate a mutation, then no more
    cdp.trigger("DOM.documentUpdated", {})

    start = asyncio.get_event_loop().time()
    await wd.wait_for_settlement(timeout_ms=2000, debounce_ms=100)
    elapsed = asyncio.get_event_loop().time() - start
    # Should wait roughly debounce_ms (100ms) for the quiet window to fire
    assert 0.08 < elapsed < 0.35, f"unexpected elapsed {elapsed:.3f}s"


@pytest.mark.asyncio
async def test_wait_returns_on_timeout_when_mutations_keep_firing():
    """If DOM keeps mutating, wait returns after timeout_ms anyway."""
    wd, cdp, _, _ = _new_watchdog()
    await wd.start()

    # Fire one mutation synchronously so the quiet event is cleared before
    # we await. Then a background task keeps firing mutations that arrive
    # faster than the debounce window can complete.
    cdp.trigger("DOM.documentUpdated", {})

    async def keep_mutating():
        for _ in range(20):
            await asyncio.sleep(0.03)
            cdp.trigger("DOM.documentUpdated", {})

    mutator = asyncio.create_task(keep_mutating())

    start = asyncio.get_event_loop().time()
    # debounce 300ms is longer than the 30ms interval between mutations,
    # so the quiet window is never reached. Wait should hit the 200ms timeout.
    await wd.wait_for_settlement(timeout_ms=200, debounce_ms=300)
    elapsed = asyncio.get_event_loop().time() - start

    mutator.cancel()
    assert 0.15 < elapsed < 0.4, f"unexpected elapsed {elapsed:.3f}s"


# ---------------------------------------------------------------------------
# Dialog handling
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_consent_dialog_accepted_and_logged():
    wd, cdp, _, dialogs = _new_watchdog()
    await wd.start()

    cdp.trigger("Page.javascriptDialogOpening", {
        "type": "confirm",
        "message": "We use cookies. Do you accept?",
    })
    # Handler runs async via create_task; yield to let it complete
    await asyncio.sleep(0.05)

    assert len(dialogs) == 1
    assert dialogs[0].auto_action == "accept_consent"

    handle_calls = [c for c in cdp.sent_commands if c[0] == "Page.handleJavaScriptDialog"]
    assert handle_calls
    assert handle_calls[-1][1] == {"accept": True}


@pytest.mark.asyncio
async def test_age_gate_dialog_dismissed_and_logged():
    wd, cdp, _, dialogs = _new_watchdog()
    await wd.start()

    cdp.trigger("Page.javascriptDialogOpening", {
        "type": "confirm",
        "message": "Please verify you are 18 or older",
    })
    await asyncio.sleep(0.05)

    assert len(dialogs) == 1
    assert dialogs[0].auto_action == "dismiss_age"


@pytest.mark.asyncio
async def test_other_dialog_dismissed_and_logged():
    wd, cdp, _, dialogs = _new_watchdog()
    await wd.start()

    cdp.trigger("Page.javascriptDialogOpening", {
        "type": "alert",
        "message": "Your cart will be cleared",
    })
    await asyncio.sleep(0.05)

    assert len(dialogs) == 1
    assert dialogs[0].auto_action == "dismiss_other"
    handle_calls = [c for c in cdp.sent_commands if c[0] == "Page.handleJavaScriptDialog"]
    assert handle_calls[-1][1] == {"accept": False}


# ---------------------------------------------------------------------------
# Network response interception
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_price_api_response_captured():
    wd, cdp, intercepted, _ = _new_watchdog()
    await wd.start()

    cdp.response_body_for_request["req-1"] = {
        "body": '{"price": 199.99, "currency": "CAD"}',
        "base64Encoded": False,
    }
    cdp.trigger("Network.responseReceived", {
        "requestId": "req-1",
        "response": {
            "url": "https://amazon.ca/api/product/B0XYZ",
            "status": 200,
            "mimeType": "application/json",
        },
    })
    await asyncio.sleep(0.05)

    assert len(intercepted) == 1
    assert intercepted[0].url == "https://amazon.ca/api/product/B0XYZ"
    assert "199.99" in intercepted[0].body
    assert intercepted[0].status == 200


@pytest.mark.asyncio
async def test_non_matching_url_not_captured():
    wd, cdp, intercepted, _ = _new_watchdog()
    await wd.start()

    cdp.trigger("Network.responseReceived", {
        "requestId": "req-2",
        "response": {"url": "https://amazon.ca/static/main.js", "status": 200, "mimeType": "text/javascript"},
    })
    await asyncio.sleep(0.05)

    assert intercepted == []


@pytest.mark.asyncio
async def test_base64_encoded_response_skipped():
    """Binary responses (images, etc.) we don't want — even if URL pattern matches."""
    wd, cdp, intercepted, _ = _new_watchdog()
    await wd.start()

    cdp.response_body_for_request["req-3"] = {
        "body": "deadbeef==",  # base64 placeholder
        "base64Encoded": True,
    }
    cdp.trigger("Network.responseReceived", {
        "requestId": "req-3",
        "response": {"url": "https://example.com/api/product/img.png", "status": 200, "mimeType": "image/png"},
    })
    await asyncio.sleep(0.05)

    assert intercepted == []


# ---------------------------------------------------------------------------
# Integration: real page
# ---------------------------------------------------------------------------

def _playwright_browser_installed() -> bool:
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return False
    return (
        os.path.isdir(os.path.expanduser("~/Library/Caches/ms-playwright"))
        or os.path.isdir(os.path.expanduser("~/.cache/ms-playwright"))
    )


@pytest.mark.skipif(
    not _playwright_browser_installed(),
    reason="Playwright Chromium not installed.",
)
@pytest.mark.asyncio
async def test_settlement_against_real_page():
    """End-to-end: open a real page via LocalPlaywrightSession, mutate the DOM,
    confirm wait_for_settlement returns within a reasonable bound."""
    from dealbot.scrapers.browser_session import LocalPlaywrightSession

    async with LocalPlaywrightSession() as bs:
        # Initial settle (page about:blank loads, fires some DOM events)
        await bs.watchdog.wait_for_settlement(timeout_ms=2000, debounce_ms=200)

        await bs.page.set_content("<html><body><div id='x'>hello</div></body></html>")
        # Trigger a DOM mutation, then immediately wait_for_settlement
        await bs.page.evaluate(
            "document.getElementById('x').innerText = 'world'"
        )
        start = asyncio.get_event_loop().time()
        await bs.watchdog.wait_for_settlement(timeout_ms=2000, debounce_ms=200)
        elapsed = asyncio.get_event_loop().time() - start
        # Should return well within timeout. Lower bound is fuzzy — depends on
        # whether the mutation event fires before we call wait. Just assert
        # we don't hit the timeout.
        assert elapsed < 1.5, f"settlement took {elapsed:.2f}s"
