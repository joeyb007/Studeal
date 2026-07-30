"""Batched embedding helper.

Persisting a hunt's listings means embedding ~100 strings at once; the
one-text-per-request client would make 100 HTTP calls. OpenAI's embeddings
endpoint accepts a list for `input`, so a batch is one request.
"""

from __future__ import annotations

import httpx
import pytest
import respx

from dealbot.llm.embeddings import EmbeddingClient, embed_texts

_URL = "https://api.openai.com/v1/embeddings"


class _SequentialOnly(EmbeddingClient):
    """Backend without a batch API — exercises the ABC default."""

    def __init__(self):
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return [float(len(text))]


@pytest.mark.asyncio
async def test_default_embed_many_is_sequential():
    client = _SequentialOnly()
    out = await client.embed_many(["a", "bb", "ccc"])
    assert out == [[1.0], [2.0], [3.0]]
    assert client.calls == 3


@pytest.mark.asyncio
@respx.mock
async def test_openai_batches_into_one_request(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    route = respx.post(_URL).mock(return_value=httpx.Response(200, json={
        "data": [
            {"embedding": [0.1] * 1536, "index": 0},
            {"embedding": [0.2] * 1536, "index": 1},
        ]
    }))
    out = await embed_texts(["macbook air", "aeron chair"])
    assert route.call_count == 1, "batch must be ONE request, not N"
    assert len(out) == 2
    assert out[0][0] == 0.1 and out[1][0] == 0.2


@pytest.mark.asyncio
@respx.mock
async def test_batch_failure_returns_empty_vectors(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    respx.post(_URL).mock(return_value=httpx.Response(500, text="boom"))
    out = await embed_texts(["a", "b"])
    assert out == [[], []], "failure must yield one empty vector per input"


@pytest.mark.asyncio
async def test_blank_inputs_skip_api(monkeypatch):
    monkeypatch.setenv("EMBEDDING_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert await embed_texts(["", "   "]) == [[], []]


@pytest.mark.asyncio
async def test_empty_list_returns_empty_list():
    assert await embed_texts([]) == []
