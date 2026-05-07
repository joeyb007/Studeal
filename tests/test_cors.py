from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from dealbot.api.main import app


@pytest.mark.asyncio
async def test_cors_preflight_allowed_origin():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.options(
            "/health",
            headers={
                "Origin": "http://localhost:3000",
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.headers.get("access-control-allow-origin") == "http://localhost:3000"


@pytest.mark.asyncio
async def test_cors_blocked_unknown_origin():
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/health", headers={"Origin": "http://evil.com"})
    assert resp.headers.get("access-control-allow-origin") != "http://evil.com"
