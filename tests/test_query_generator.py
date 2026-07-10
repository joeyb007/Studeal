"""Tests for QueryGenerator."""

from __future__ import annotations

import json

import pytest

from dealbot.agents.query_generator import QueryGenerator
from dealbot.schemas import WatchlistContext


class _MockResponse:
    def __init__(self, content: str) -> None:
        self.content = content


class _MockLLM:
    def __init__(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def complete(self, messages, response_format=None, **kwargs):
        if self._responses:
            return _MockResponse(self._responses.pop(0))
        return _MockResponse('{"queries":[]}')


class _RaisingLLM:
    async def complete(self, messages, response_format=None, **kwargs):
        raise RuntimeError("LLM down")


def _spec() -> WatchlistContext:
    return WatchlistContext(product_query="Herman Miller Aeron", max_budget=700.0)


@pytest.mark.asyncio
async def test_returns_parsed_queries():
    llm = _MockLLM([json.dumps({"queries": [
        "Herman Miller Aeron", "Aeron chair used", "Aeron office chair",
    ]})])
    gen = QueryGenerator(llm=llm)
    queries = await gen.generate(_spec())
    assert queries == ["Herman Miller Aeron", "Aeron chair used", "Aeron office chair"]


@pytest.mark.asyncio
async def test_dedupes_case_insensitively_and_strips():
    llm = _MockLLM([json.dumps({"queries": [
        "Aeron", "aeron", " Aeron ", "Herman Miller Aeron",
    ]})])
    gen = QueryGenerator(llm=llm)
    queries = await gen.generate(_spec())
    # "Aeron", "aeron", " Aeron " all normalize to "aeron" — one entry only.
    assert queries == ["Aeron", "Herman Miller Aeron"]


@pytest.mark.asyncio
async def test_caps_at_configured_count():
    llm = _MockLLM([json.dumps({"queries": ["a", "b", "c", "d", "e"]})])
    gen = QueryGenerator(llm=llm, count=3)
    queries = await gen.generate(_spec())
    assert len(queries) == 3


@pytest.mark.asyncio
async def test_llm_failure_falls_back_to_original_query():
    gen = QueryGenerator(llm=_RaisingLLM())
    queries = await gen.generate(_spec())
    assert queries == ["Herman Miller Aeron"]


@pytest.mark.asyncio
async def test_malformed_json_falls_back_to_original_query():
    llm = _MockLLM(["not json"])
    gen = QueryGenerator(llm=llm)
    queries = await gen.generate(_spec())
    assert queries == ["Herman Miller Aeron"]


@pytest.mark.asyncio
async def test_empty_llm_output_falls_back_to_original_query():
    llm = _MockLLM([json.dumps({"queries": []})])
    gen = QueryGenerator(llm=llm)
    queries = await gen.generate(_spec())
    assert queries == ["Herman Miller Aeron"]
