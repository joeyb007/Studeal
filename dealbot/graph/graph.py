from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph

from dealbot.graph.nodes import ingest_node, persist_node, score_node
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient


def _route_after_score(state: PipelineState) -> str:
    """Conditional edge: skip persist if scoring failed."""
    if "error" in state:
        return END
    return "persist"


def build_graph(llm: LLMClient) -> StateGraph:
    """
    Assembles and compiles the DealBot pipeline graph.

    ingest → score → persist
                ↓ (on error)
               END

    Args:
        llm: The LLMClient to use for the score node.

    Returns:
        A compiled LangGraph app ready to invoke.
    """
    # Bind the llm dependency into score_node so LangGraph sees a plain (state) -> state function
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
