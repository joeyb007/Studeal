from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from dealbot.graph.nodes import (
    ingest_node,
    keyword_dedup_node,
    orchestrator_node,
    persist_node,
    score_node,
)
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient
from dealbot.schemas import DealRaw


def _route_after_score(state: PipelineState) -> str:
    """Conditional edge: skip persist if scoring failed."""
    if "error" in state:
        return END
    return "persist"


def _route_after_dedup(state: PipelineState) -> str:
    """Skip the rest of the pipeline if this keyword was already covered today."""
    if state.get("keyword_covered"):
        return END
    return "query_gen"


def _fan_out_to_score(state: PipelineState) -> list[Send]:
    """Fan out one score invocation per extracted candidate."""
    candidates: list[DealRaw] = state.get("candidates", [])
    return [Send("score", {**state, "deal": candidate}) for candidate in candidates]


def build_graph(llm: LLMClient) -> StateGraph:
    """
    Scorer-only pipeline (used by existing Celery task).

    ingest → score → persist
                ↓ (on error)
               END
    """
    bound_score_node = functools.partial(score_node, llm=llm)

    graph = StateGraph(PipelineState)

    graph.add_node("ingest", ingest_node)
    graph.add_node("score", bound_score_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "ingest")
    graph.add_edge("ingest", "score")
    graph.add_conditional_edges("score", _route_after_score, {"persist": "persist", END: END})
    graph.add_edge("persist", END)

    return graph.compile()


def build_hunter_graph(llm: LLMClient) -> StateGraph:
    """
    Full hunter pipeline — entry point is a watchlist keyword.

    keyword_dedup → orchestrator → Send() → score → persist
                         ↓ (keyword already covered)
                        END
    """
    bound_orchestrator = functools.partial(orchestrator_node, llm=llm)
    bound_score = functools.partial(score_node, llm=llm)

    graph = StateGraph(PipelineState)

    graph.add_node("keyword_dedup", keyword_dedup_node)
    graph.add_node("orchestrator", bound_orchestrator)
    graph.add_node("score", bound_score)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "keyword_dedup")
    graph.add_conditional_edges(
        "keyword_dedup",
        _route_after_dedup,
        {"orchestrator": "orchestrator", END: END},
    )
    graph.add_conditional_edges("orchestrator", _fan_out_to_score, ["score"])
    graph.add_conditional_edges("score", _route_after_score, {"persist": "persist", END: END})
    graph.add_edge("persist", END)

    return graph.compile()
