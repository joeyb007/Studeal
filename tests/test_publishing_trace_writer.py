"""Tests for PublishingTraceWriter — the trace→event bridge.

Wraps any TraceWriter: every call forwards to the inner writer unchanged;
explorer turns/screenshots/errors additionally publish events via
publish_nowait. Oversized screenshots are dropped from the stream (the inner
writer still records them to disk).
"""

from __future__ import annotations

import asyncio
import json

import pytest

from dealbot.agents.tracing import NullTraceWriter, PublishingTraceWriter
from dealbot.events.publisher import RedisEventPublisher


class FakeRedis:
    def __init__(self):
        self.published: list[tuple[str, str]] = []

    async def publish(self, channel: str, message: str):
        self.published.append((channel, message))

    async def aclose(self):
        pass


class RecordingInner(NullTraceWriter):
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def record_page_reader_turn(self, **kw):
        self.calls.append(("turn", kw))

    def record_screenshot(self, **kw):
        self.calls.append(("screenshot", kw))

    def record_error(self, **kw):
        self.calls.append(("error", kw))

    def finalize(self):
        self.calls.append(("finalize", {}))


def _writer(fake):
    pub = RedisEventPublisher(client=fake)
    inner = RecordingInner()
    writer = PublishingTraceWriter(
        inner, pub, hunt_id=1, watchlist_id=2, query="aeron", marketplace="kijiji",
    )
    return inner, writer


@pytest.mark.asyncio
async def test_turn_forwards_to_inner_and_publishes():
    fake = FakeRedis()
    inner, w = _writer(fake)
    w.record_page_reader_turn(
        orchestrator_turn=0, sub_turn=3, url="https://k.ca/serp",
        snapshot_text="x", element_map_size=10, prompt=[],
        response_content=None, action_summary="click next",
        result_summary="page 2 loaded" * 50,
    )
    await asyncio.sleep(0)
    assert inner.calls[0][0] == "turn"
    assert len(fake.published) == 1
    body = json.loads(fake.published[0][1])
    assert body["type"] == "explorer.turn"
    assert body["turn"] == 3
    assert body["marketplace"] == "kijiji"
    assert len(body["result"]) <= 200


@pytest.mark.asyncio
async def test_screenshot_published_as_data_url():
    fake = FakeRedis()
    inner, w = _writer(fake)
    w.record_screenshot(
        orchestrator_turn=1, sub_turn=None, label="viewport", png_bytes=b"\x89PNGdata",
    )
    await asyncio.sleep(0)
    assert inner.calls[0][0] == "screenshot"
    body = json.loads(fake.published[0][1])
    assert body["type"] == "explorer.screenshot"
    assert body["image_data_url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio
async def test_oversized_screenshot_dropped_but_forwarded():
    fake = FakeRedis()
    inner, w = _writer(fake)
    w.record_screenshot(
        orchestrator_turn=1, sub_turn=None, label="viewport",
        png_bytes=b"\x89PNG" + b"0" * 300_000,
    )
    await asyncio.sleep(0)
    assert inner.calls[0][0] == "screenshot"  # inner still records
    assert fake.published == []               # event dropped: data URL too large


@pytest.mark.asyncio
async def test_error_forwards_and_publishes():
    fake = FakeRedis()
    inner, w = _writer(fake)
    w.record_error(orchestrator_turn=0, worker="explorer", error="captcha wall")
    await asyncio.sleep(0)
    assert inner.calls[0][0] == "error"
    body = json.loads(fake.published[0][1])
    assert body["type"] == "explorer.error" and body["error"] == "captcha wall"


@pytest.mark.asyncio
async def test_finalize_and_orchestrator_turn_forward_only():
    fake = FakeRedis()
    inner, w = _writer(fake)
    w.record_orchestrator_turn(
        turn=1, prompt=[], response_content=None,
        decision_summary="d", worker_chosen="explorer",
    )
    w.finalize()
    await asyncio.sleep(0)
    assert ("finalize", {}) in inner.calls
    assert fake.published == []
