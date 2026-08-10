"""On-demand fetch seams (copilot spec 2026-08-10, phase D): extraction
validation and the honest-failure paths, with the browser and LLM stubbed."""

import json

import pytest

from dealbot.agents import fetch_listing
from dealbot.llm.base import LLMResponse


class _FakeLLM:
    def __init__(self, payload: dict):
        self._payload = payload

    async def complete(self, messages, tools=None, response_format=None):
        return LLMResponse(content=json.dumps(self._payload))


def _stub_visit(monkeypatch, result):
    async def _visit(url, marketplace):
        return result
    monkeypatch.setattr(fetch_listing, "_visit_url", _visit)


@pytest.mark.asyncio
async def test_unsupported_marketplace_is_none(monkeypatch):
    assert await fetch_listing.fetch_and_persist("https://x.com/a", "craigslist") is None


@pytest.mark.asyncio
async def test_navigation_failure_is_none(monkeypatch):
    _stub_visit(monkeypatch, None)
    assert await fetch_listing.fetch_and_persist("https://www.kijiji.ca/v-x/1", "kijiji") is None


@pytest.mark.asyncio
async def test_unavailable_page_is_none(monkeypatch):
    _stub_visit(monkeypatch, ("This ad is no longer available.", None))
    monkeypatch.setattr(fetch_listing, "_extract_llm", lambda: _FakeLLM(
        {"available": False, "title": "", "price": None, "currency": "CAD",
         "condition": "unknown", "location": None}
    ))
    assert await fetch_listing.fetch_and_persist("https://www.kijiji.ca/v-x/1", "kijiji") is None


@pytest.mark.asyncio
async def test_bad_price_is_none(monkeypatch):
    _stub_visit(monkeypatch, ("page text", None))
    monkeypatch.setattr(fetch_listing, "_extract_llm", lambda: _FakeLLM(
        {"available": True, "title": "Nintendo Switch", "price": 0,
         "currency": "CAD", "condition": "used", "location": "Toronto"}
    ))
    assert await fetch_listing.fetch_and_persist("https://www.kijiji.ca/v-x/1", "kijiji") is None


@pytest.mark.asyncio
async def test_good_extraction_persists(monkeypatch, db_factory):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _s():
        async with db_factory() as session:
            yield session

    monkeypatch.setattr("dealbot.db.database.get_async_session", _s)
    monkeypatch.setattr("dealbot.persistence.listings.get_async_session", _s)

    async def _no_embeddings(offers):
        return [[] for _ in offers]
    monkeypatch.setattr("dealbot.persistence.listings._embeddings_for", _no_embeddings)

    _stub_visit(monkeypatch, ("page text", "https://img.example/1.jpg"))
    monkeypatch.setattr(fetch_listing, "_extract_llm", lambda: _FakeLLM(
        {"available": True, "title": "Nintendo Switch OLED", "price": 249,
         "currency": "CAD", "condition": "used", "location": "Markham"}
    ))
    listing = await fetch_listing.fetch_and_persist(
        "https://www.kijiji.ca/v-x/1741643675", "kijiji",
    )
    assert listing is not None
    assert listing.title == "Nintendo Switch OLED"
    assert listing.price == 249.0
    assert listing.image_url == "https://img.example/1.jpg"
    assert listing.marketplace == "kijiji"
