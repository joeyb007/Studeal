"""Workers for the DealHuntOrchestrator.

The orchestrator dispatches one of these per turn:
  - SearchPlanner  — single LLM call, seeds the frontier with starting leads
  - PageReader     — tool-using SUBAGENT, mini ReAct loop with browser tools
  - LeadScorer     — single LLM call, scores a new lead 0-1
  - OfferExtractor — single LLM call, structured extraction (Pydantic)
  - Validator      — single LLM call, final acceptance + replan feedback

Every worker takes `LLMClient` via constructor (DI). This makes the eval suite
trivial — swap in any LLMClient subclass per role.
"""

from dealbot.agents.workers.lead_scorer import LeadScorer
from dealbot.agents.workers.offer_extractor import OfferExtractor
from dealbot.agents.workers.page_reader import PageReader, PageReaderResult
from dealbot.agents.workers.search_planner import SearchPlanner
from dealbot.agents.workers.validator import ValidationDecision, Validator

__all__ = [
    "LeadScorer",
    "OfferExtractor",
    "PageReader",
    "PageReaderResult",
    "SearchPlanner",
    "ValidationDecision",
    "Validator",
]
