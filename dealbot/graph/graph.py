from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from dealbot.graph.nodes import (
    extract_node,
    fetch_node,
    hunt_node,
    ingest_node,
    persist_node,
    query_gen_node,
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

    query_gen → hunt → fetch → extract → Send() → score → persist
                                                      ↓ (on error)
                                                     END
    """
    bound_query_gen = functools.partial(query_gen_node, llm=llm)
    bound_extract = functools.partial(extract_node, llm=llm)
    bound_score = functools.partial(score_node, llm=llm)

    graph = StateGraph(PipelineState)

    graph.add_node("query_gen", bound_query_gen)
    graph.add_node("hunt", hunt_node)
    graph.add_node("fetch", fetch_node)
    graph.add_node("extract", bound_extract)
    graph.add_node("score", bound_score)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "query_gen")
    graph.add_edge("query_gen", "hunt")
    graph.add_edge("hunt", "fetch")
    graph.add_edge("fetch", "extract")
    graph.add_conditional_edges("extract", _fan_out_to_score, ["score"])
    graph.add_conditional_edges("score", _route_after_score, {"persist": "persist", END: END})
    graph.add_edge("persist", END)

    return graph.compile()
