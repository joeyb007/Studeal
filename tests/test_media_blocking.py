"""Media blocking + agentcore backend config — no network, no real browser."""

from __future__ import annotations

import pytest

from dealbot.scrapers.browser_session import (
    AgentCoreBrowserSession,
    BrowserbaseSession,
    LocalPlaywrightSession,
    build_browser_session,
    media_blocking_enabled,
    should_abort_request,
)
from dealbot.scrapers.agentcore_session import agentcore_region


def test_blocked_resource_types():
    assert should_abort_request("image") is True
    assert should_abort_request("media") is True
    assert should_abort_request("font") is True


def test_kept_resource_types():
    # CSS drives perception bboxes; JS renders SPA listings; documents and
    # XHR are the page itself.
    for kept in ("document", "stylesheet", "script", "xhr", "fetch", "websocket"):
        assert should_abort_request(kept) is False


def test_blocking_enabled_by_default(monkeypatch):
    monkeypatch.delenv("AGENT_BLOCK_MEDIA", raising=False)
    assert media_blocking_enabled() is True


def test_blocking_rollback_lever(monkeypatch):
    monkeypatch.setenv("AGENT_BLOCK_MEDIA", "0")
    assert media_blocking_enabled() is False


class _FakeRequest:
    def __init__(self, resource_type: str) -> None:
        self.resource_type = resource_type


class _FakeRoute:
    def __init__(self, resource_type: str) -> None:
        self.request = _FakeRequest(resource_type)
        self.aborted = False
        self.continued = False

    async def abort(self) -> None:
        self.aborted = True

    async def continue_(self) -> None:
        self.continued = True


class _FakeContext:
    def __init__(self) -> None:
        self.handler = None

    async def route(self, pattern: str, handler) -> None:
        self.handler = handler


@pytest.mark.asyncio
async def test_route_handler_aborts_images_continues_documents(monkeypatch):
    monkeypatch.delenv("AGENT_BLOCK_MEDIA", raising=False)
    session = LocalPlaywrightSession()
    ctx = _FakeContext()
    await session._apply_media_blocking(ctx)
    assert ctx.handler is not None

    img = _FakeRoute("image")
    await ctx.handler(img)
    assert img.aborted and not img.continued

    doc = _FakeRoute("document")
    await ctx.handler(doc)
    assert doc.continued and not doc.aborted


@pytest.mark.asyncio
async def test_blocking_disabled_registers_no_route(monkeypatch):
    monkeypatch.setenv("AGENT_BLOCK_MEDIA", "0")
    session = LocalPlaywrightSession()
    ctx = _FakeContext()
    await session._apply_media_blocking(ctx)
    assert ctx.handler is None


def test_backend_selection(monkeypatch):
    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p")
    assert isinstance(build_browser_session("agentcore"), AgentCoreBrowserSession)
    assert isinstance(build_browser_session("browserbase"), BrowserbaseSession)
    assert isinstance(build_browser_session("local"), LocalPlaywrightSession)
    with pytest.raises(ValueError):
        build_browser_session("nope")


def test_marketplace_backend_split():
    # Browserbase cancelled 2026-08-16: nothing in the active lineup may
    # pin it; the fingerprint/residential sites are parked in _DISABLED.
    from dealbot.agents.marketplace_router import CONFIG_BY_KEY, _DISABLED_MARKETPLACES

    assert all(m.backend is None for m in CONFIG_BY_KEY.values())
    parked = {m.key for m in _DISABLED_MARKETPLACES}
    # Browserbase-only sites stay parked; FB returned on agentcore+residential.
    assert {"bestbuy_outlet", "visions_openbox"} <= parked
    assert "fb_marketplace" not in parked


def test_session_from_env_honors_override(monkeypatch):
    from dealbot.agents.composition import build_session_from_env

    monkeypatch.setenv("AGENT_BROWSER_BACKEND", "agentcore")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "k")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "p")
    assert isinstance(build_session_from_env(), AgentCoreBrowserSession)
    assert isinstance(build_session_from_env(backend="browserbase"), BrowserbaseSession)


def test_residential_proxy_flag_reaches_agentcore_session(monkeypatch):
    from dealbot.agents.composition import build_session_from_env

    monkeypatch.setenv("AGENT_BROWSER_BACKEND", "agentcore")
    plain = build_session_from_env()
    proxied = build_session_from_env(residential_proxy=True)
    assert plain._proxy is False
    assert proxied._proxy is True


def test_fb_configured_for_residential_proxy():
    # FB is the sole residential-proxy user, on agentcore (no browserbase).
    from dealbot.agents.marketplace_router import CONFIG_BY_KEY

    fb = CONFIG_BY_KEY["fb_marketplace"]
    assert fb.residential_proxy is True
    assert fb.backend is None
    assert all(
        not m.residential_proxy for k, m in CONFIG_BY_KEY.items() if k != "fb_marketplace"
    )


def test_agentcore_region_resolution(monkeypatch):
    monkeypatch.delenv("AGENTCORE_REGION", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    assert agentcore_region() == "us-east-1"
    monkeypatch.setenv("AGENTCORE_REGION", "us-west-2")
    assert agentcore_region() == "us-west-2"
