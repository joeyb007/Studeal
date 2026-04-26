from __future__ import annotations

from typing import NotRequired, TypedDict

from dealbot.schemas import DealRaw, DealScore


class PipelineState(TypedDict):
    """State object that flows through every node in the LangGraph pipeline."""

    # --- scorer flow (build_graph) ---
    deal: NotRequired[DealRaw]
    score_result: NotRequired[DealScore]
    embedding: NotRequired[list[float]]
    error: NotRequired[str]

    # --- hunter flow (build_hunter_graph) ---
    keyword: NotRequired[str]
    candidates: NotRequired[list[DealRaw]]
    keyword_covered: NotRequired[bool]
