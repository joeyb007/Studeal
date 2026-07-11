"""Tests for RerankService — Cohere Rerank v3.5 wrapper."""

from __future__ import annotations

import httpx
import pytest
import respx

from dealbot.rerank.service import RerankResult, RerankService


_COHERE_URL = "https://api.cohere.com/v2/rerank"


@pytest.mark.asyncio
async def test_returns_parsed_rerank_response():
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_COHERE_URL).respond(
            200,
            json={
                "results": [
                    {"index": 2, "relevance_score": 0.95},
                    {"index": 0, "relevance_score": 0.42},
                    {"index": 1, "relevance_score": 0.11},
                ],
            },
        )
        service = RerankService(api_key="test-key")
        docs = ["a", "b", "c"]
        result = await service.rerank("aeron chair", docs, top_n=3)

    assert len(result) == 3
    assert result[0] == RerankResult(index=2, relevance_score=0.95)
    assert result[1] == RerankResult(index=0, relevance_score=0.42)
    assert result[2] == RerankResult(index=1, relevance_score=0.11)


@pytest.mark.asyncio
async def test_empty_documents_returns_empty():
    service = RerankService(api_key="test-key")
    assert await service.rerank("query", []) == []


@pytest.mark.asyncio
async def test_missing_api_key_returns_identity_fallback():
    service = RerankService(api_key="")
    docs = ["a", "b", "c", "d"]
    result = await service.rerank("query", docs, top_n=2)

    assert len(result) == 2
    assert [r.index for r in result] == [0, 1]
    assert all(r.relevance_score == 0.0 for r in result)


@pytest.mark.asyncio
async def test_api_failure_returns_identity_fallback():
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_COHERE_URL).mock(side_effect=httpx.ConnectError("network"))
        service = RerankService(api_key="test-key")
        result = await service.rerank("query", ["a", "b", "c"], top_n=5)

    assert len(result) == 3  # clamped to doc count
    assert all(r.relevance_score == 0.0 for r in result)


@pytest.mark.asyncio
async def test_5xx_returns_identity_fallback():
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_COHERE_URL).respond(503)
        service = RerankService(api_key="test-key")
        result = await service.rerank("query", ["a", "b"], top_n=5)

    assert len(result) == 2
    assert all(r.relevance_score == 0.0 for r in result)


@pytest.mark.asyncio
async def test_malformed_response_returns_empty_result_list():
    """Cohere returns unexpected shape → we skip malformed rows, not crash."""
    async with respx.mock(assert_all_called=False) as mock:
        mock.post(_COHERE_URL).respond(
            200,
            json={"results": [
                {"index": 0, "relevance_score": 0.5},
                {"index": "bogus", "relevance_score": 0.4},
                {"relevance_score": 0.3},
            ]},
        )
        service = RerankService(api_key="test-key")
        result = await service.rerank("query", ["a", "b", "c"])

    assert len(result) == 1
    assert result[0].index == 0


@pytest.mark.asyncio
async def test_top_n_clamped_to_documents_count():
    """top_n=100 but only 3 docs → API request sent with top_n=3."""
    async with respx.mock(assert_all_called=True) as mock:
        route = mock.post(_COHERE_URL).respond(200, json={"results": []})
        service = RerankService(api_key="test-key")
        await service.rerank("query", ["a", "b", "c"], top_n=100)

        payload = route.calls[0].request.content
    import json as _json
    body = _json.loads(payload)
    assert body["top_n"] == 3
