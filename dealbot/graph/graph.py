from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph

from dealbot.graph.nodes import (
    ingest_node,
    persist_node,
    score_and_persist_node,
    score_node,
)
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient


def _route_after_score(state: PipelineState) -> str:
    """Conditional edge: skip persist if scoring failed."""
    if "error" in state:
        return END
    return "persist"


def build_graph(llm: LLMClient) -> StateGraph:
    """Original scorer pipeline: ingest → score → persist."""
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


def build_scorer_graph(llm: LLMClient) -> StateGraph:
    """Single-node graph that runs score_and_persist on one deal.

    Called by the ResearchAgent's downstream fan-out after the ReAct loop
    accumulates deals.
    """
    bound = functools.partial(score_and_persist_node, llm=llm)

    graph = StateGraph(PipelineState)
    graph.add_node("score_and_persist", bound)
    graph.add_edge(START, "score_and_persist")
    graph.add_edge("score_and_persist", END)
    return graph.compile()
