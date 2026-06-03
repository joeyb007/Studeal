from __future__ import annotations

from typing import NotRequired, TypedDict

from dealbot.schemas import DealRaw


class PipelineState(TypedDict):
    """State object that flows through every node in the LangGraph pipeline."""

    deal: NotRequired[DealRaw]
    embedding: NotRequired[list[float]]
    error: NotRequired[str]

    # --- hunter flow (build_hunter_graph) ---
    keyword: NotRequired[str]
    candidates: NotRequired[list[DealRaw]]
    keyword_covered: NotRequired[bool]
    hunt_cost_usd: NotRequired[float]
