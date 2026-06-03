from __future__ import annotations

import functools

from langgraph.graph import END, START, StateGraph

from dealbot.graph.nodes import score_and_persist_node
from dealbot.graph.state import PipelineState
from dealbot.llm.base import LLMClient  # noqa: F401 — re-exported for callers


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
