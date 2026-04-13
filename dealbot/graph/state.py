from __future__ import annotations

from typing import NotRequired, TypedDict

from dealbot.schemas import DealRaw, DealScore
from dealbot.search.client import FetchedPage, SearchResult


class PipelineState(TypedDict):
    """State object that flows through every node in the LangGraph pipeline."""

    # --- scorer flow (existing) ---
    deal: NotRequired[DealRaw]
    score_result: NotRequired[DealScore]
    embedding: NotRequired[list[float]]
    error: NotRequired[str]

    # --- hunter flow (Phase 7) ---
    keyword: NotRequired[str]
    queries: NotRequired[list[str]]
    search_results: NotRequired[list[SearchResult]]
    fetched_pages: NotRequired[list[FetchedPage]]
    candidates: NotRequired[list[DealRaw]]
