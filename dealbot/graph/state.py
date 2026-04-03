from __future__ import annotations

from typing import NotRequired, TypedDict

from dealbot.schemas import DealRaw, DealScore


class PipelineState(TypedDict):
    """State object that flows through every node in the LangGraph pipeline."""

    deal: DealRaw
    score_result: NotRequired[DealScore]
    error: NotRequired[str]
