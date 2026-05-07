from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from dealbot.worker.seed import _run_seed
from dealbot.worker.tasks import _run_hunter


@pytest.mark.asyncio
async def test_seed_processes_all_queries():
    """All seed queries are processed; an error in one doesn't abort the rest."""
    call_log: list[str] = []
    error_query = "cookware set sale first apartment"

    async def fake_invoke(state: dict) -> dict:
        kw = state["keyword"]
        call_log.append(kw)
        if kw == error_query:
            raise RuntimeError("simulated scrape failure")
        return {"keyword_covered": False}

    mock_graph = MagicMock()
    mock_graph.ainvoke = fake_invoke

    with patch("dealbot.worker.seed.build_hunter_graph", return_value=mock_graph):
        result = await _run_seed(MagicMock())

    from dealbot.worker.seed import SEED_QUERIES

    assert len(call_log) == len(SEED_QUERIES), "Every query must be attempted"
    assert result["errors"] == 1
    assert result["processed"] + result["skipped"] == len(SEED_QUERIES) - 1


@pytest.mark.asyncio
async def test_seed_respects_semaphore():
    """No more than 3 seed queries run concurrently."""
    concurrent_count = 0
    peak_concurrent = 0
    lock = asyncio.Lock()

    async def fake_invoke(state: dict) -> dict:
        nonlocal concurrent_count, peak_concurrent
        async with lock:
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
        await asyncio.sleep(0.01)
        async with lock:
            concurrent_count -= 1
        return {"keyword_covered": False}

    mock_graph = MagicMock()
    mock_graph.ainvoke = fake_invoke

    with patch("dealbot.worker.seed.build_hunter_graph", return_value=mock_graph):
        await _run_seed(MagicMock())

    assert peak_concurrent >= 2, f"Peak concurrent was {peak_concurrent} — pipeline is sequential, not concurrent"
    assert peak_concurrent <= 3, f"Peak concurrent was {peak_concurrent}, expected ≤ 3"


def _make_kw(text: str):
    from dealbot.db.models import WatchlistKeyword
    kw = WatchlistKeyword()
    kw.keyword = text
    return kw


def _mock_session(keywords: list):
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    execute_result = MagicMock()
    execute_result.scalars.return_value.all.return_value = keywords
    mock_session.execute = AsyncMock(return_value=execute_result)
    return mock_session


@pytest.mark.asyncio
async def test_hunt_processes_all_keywords():
    """All watchlist keywords are processed; one error doesn't abort the batch."""
    keywords = [_make_kw(f"keyword-{i}") for i in range(6)]
    error_kw = "keyword-2"
    call_log: list[str] = []

    async def fake_invoke(state: dict) -> dict:
        kw = state["keyword"]
        call_log.append(kw)
        if kw == error_kw:
            raise RuntimeError("simulated failure")
        return {"keyword_covered": False}

    mock_graph = MagicMock()
    mock_graph.ainvoke = fake_invoke

    with (
        patch("dealbot.worker.tasks.build_hunter_graph", return_value=mock_graph),
        patch("dealbot.worker.tasks.get_async_session", return_value=_mock_session(keywords)),
    ):
        result = await _run_hunter(MagicMock())

    assert len(call_log) == 6
    assert result["errors"] == 1
    assert result["processed"] == 5


@pytest.mark.asyncio
async def test_hunt_respects_semaphore():
    """No more than 3 hunt keywords run concurrently."""
    keywords = [_make_kw(f"kw-{i}") for i in range(9)]
    concurrent_count = 0
    peak_concurrent = 0
    counter_lock = asyncio.Lock()

    async def fake_invoke(state: dict) -> dict:
        nonlocal concurrent_count, peak_concurrent
        async with counter_lock:
            concurrent_count += 1
            peak_concurrent = max(peak_concurrent, concurrent_count)
        await asyncio.sleep(0.01)
        async with counter_lock:
            concurrent_count -= 1
        return {"keyword_covered": False}

    mock_graph = MagicMock()
    mock_graph.ainvoke = fake_invoke

    with (
        patch("dealbot.worker.tasks.build_hunter_graph", return_value=mock_graph),
        patch("dealbot.worker.tasks.get_async_session", return_value=_mock_session(keywords)),
    ):
        await _run_hunter(MagicMock())

    assert peak_concurrent >= 2, f"Peak concurrent was {peak_concurrent} — pipeline is sequential, not concurrent"
    assert peak_concurrent <= 3, f"Peak concurrent was {peak_concurrent}, expected ≤ 3"
