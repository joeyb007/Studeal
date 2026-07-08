"""Tests for ExtractorPool — async worker pool consuming a shared queue."""

from __future__ import annotations

import asyncio

import pytest

from dealbot.agents.extractor_pool import ExtractorPool
from dealbot.agents.perception import PageSnapshot
from dealbot.agents.workers.extractor import Offer
from dealbot.schemas import WatchlistContext


class _MockExtractor:
    def __init__(self, per_call: list[list[Offer]] | None = None) -> None:
        self.per_call = list(per_call or [])
        self.call_count = 0

    async def extract_from_snapshot(self, snap, marketplace, spec):
        self.call_count += 1
        if self.per_call:
            return self.per_call.pop(0)
        return []


class _RaisingExtractor:
    async def extract_from_snapshot(self, snap, marketplace, spec):
        raise RuntimeError("simulated failure")


def _snap(url: str) -> PageSnapshot:
    return PageSnapshot(text="listings", element_map={}, url=url, title="", char_count=8)


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Aeron", max_budget=700.0)


def _offer(url: str) -> Offer:
    return Offer(title="Aeron", price=500.0, currency="CAD", url=url, marketplace="kijiji")


@pytest.mark.asyncio
async def test_pool_processes_all_submitted_tasks():
    """Submit 3 tasks → all extracted → drain returns concatenated offers."""
    extractor = _MockExtractor([
        [_offer("https://kijiji.ca/l/1"), _offer("https://kijiji.ca/l/2")],
        [_offer("https://kijiji.ca/l/3")],
        [_offer("https://kijiji.ca/l/4"), _offer("https://kijiji.ca/l/5")],
    ])
    pool = ExtractorPool(extractor=extractor, num_workers=2)
    await pool.start()
    await pool.submit(_snap("u1"), "kijiji", _spec())
    await pool.submit(_snap("u2"), "kijiji", _spec())
    await pool.submit(_snap("u3"), "kijiji", _spec())
    offers = await pool.drain()

    assert extractor.call_count == 3
    urls = {o.url for o in offers}
    assert urls == {
        "https://kijiji.ca/l/1", "https://kijiji.ca/l/2", "https://kijiji.ca/l/3",
        "https://kijiji.ca/l/4", "https://kijiji.ca/l/5",
    }


@pytest.mark.asyncio
async def test_pool_survives_extractor_failure():
    """Raising extractor doesn't kill workers; pool drains cleanly."""
    pool = ExtractorPool(extractor=_RaisingExtractor(), num_workers=2)
    await pool.start()
    await pool.submit(_snap("u1"), "kijiji", _spec())
    await pool.submit(_snap("u2"), "kijiji", _spec())
    offers = await pool.drain()
    assert offers == []


@pytest.mark.asyncio
async def test_pool_drain_returns_empty_when_never_submitted():
    pool = ExtractorPool(extractor=_MockExtractor(), num_workers=2)
    await pool.start()
    offers = await pool.drain()
    assert offers == []


@pytest.mark.asyncio
async def test_submit_before_start_raises():
    pool = ExtractorPool(extractor=_MockExtractor(), num_workers=2)
    with pytest.raises(RuntimeError, match="submit\\(\\) before start\\(\\)"):
        await pool.submit(_snap("u1"), "kijiji", _spec())
