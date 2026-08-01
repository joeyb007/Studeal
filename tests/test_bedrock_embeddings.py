"""Titan V2 embeddings behind the EmbeddingClient ABC.

Contract parity with the OpenAI client: [] on any failure (a hunt must never
lose listings because embeddings are down), positional alignment in
embed_many. Titan has no batch endpoint, so embed_many is a bounded fan-out.
"""

from __future__ import annotations

import json

import pytest

from dealbot.llm.bedrock_client import BedrockEmbeddingClient


class _FakeBody:
    def __init__(self, payload: dict):
        self._payload = json.dumps(payload).encode()

    async def read(self) -> bytes:
        return self._payload


class _FakeRuntime:
    def __init__(self, vector: list[float] | None = None, fail_on: set[str] | None = None):
        self.vector = vector if vector is not None else [0.1] * 1024
        self.fail_on = fail_on or set()
        self.calls: list[dict] = []

    async def invoke_model(self, **kwargs):
        self.calls.append(kwargs)
        body = json.loads(kwargs["body"])
        if body["inputText"] in self.fail_on:
            raise RuntimeError("titan down for this input")
        return {"body": _FakeBody({"embedding": self.vector})}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _client(fake: _FakeRuntime) -> BedrockEmbeddingClient:
    client = BedrockEmbeddingClient()
    client._client_factory = lambda: fake
    return client


@pytest.mark.asyncio
async def test_embed_requests_1024_normalized_and_returns_vector():
    fake = _FakeRuntime()
    client = _client(fake)

    vector = await client.embed("used laptop")

    body = json.loads(fake.calls[0]["body"])
    assert body == {"inputText": "used laptop", "dimensions": 1024, "normalize": True}
    assert len(vector) == 1024


@pytest.mark.asyncio
async def test_embed_returns_empty_on_failure():
    fake = _FakeRuntime(fail_on={"boom"})
    client = _client(fake)
    assert await client.embed("boom") == []


@pytest.mark.asyncio
async def test_embed_many_keeps_positional_alignment_through_failures():
    """One failed input must not shift its neighbours' vectors."""
    fake = _FakeRuntime(fail_on={"b"})
    client = _client(fake)

    vectors = await client.embed_many(["a", "b", "c"])

    assert len(vectors) == 3
    assert len(vectors[0]) == 1024
    assert vectors[1] == [], "the failed slot is empty, not dropped"
    assert len(vectors[2]) == 1024


@pytest.mark.asyncio
async def test_embed_many_empty_input():
    assert await _client(_FakeRuntime()).embed_many([]) == []
