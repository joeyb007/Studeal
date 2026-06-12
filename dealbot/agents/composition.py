"""Composition root for the DealHuntOrchestrator.

Two factory functions, one for production (Groq LLama 3.3 70B everywhere,
Browserbase session) and one for the eval suite (any LLMClient mix +
LocalPlaywrightSession). Both produce a fully-wired DealHuntOrchestrator
ready to `await orchestrator.run(spec)`.

This is the single place where worker → llm wiring happens. If we ever
swap the orchestrator to a fine-tuned 7B, only the production factory needs touching.
"""

from __future__ import annotations

import logging
import os
from typing import Callable

from dealbot.agents.orchestrator import DealHuntOrchestrator, SessionFactory
from dealbot.agents.tools import DomainRateLimiter, all_tools
from dealbot.agents.workers import (
    LeadScorer,
    OfferExtractor,
    PageReader,
    SearchPlanner,
    Validator,
)
from dealbot.llm.base import LLMClient
from dealbot.llm.groq_client import GroqClient
from dealbot.scrapers.browser_session import (
    BrowserbaseSession,
    LocalPlaywrightSession,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Default Groq models — Llama 3.3 70B everywhere in v1 (cheap on Groq's free
# tier). Stretch swap point: replace orchestrator_llm + page_reader_llm with
# a fine-tuned 7B once Phase 4 lands.
# ---------------------------------------------------------------------------

_GROQ_70B = "llama-3.3-70b-versatile"
_GROQ_8B = "llama-3.1-8b-instant"


def _default_groq(model: str) -> GroqClient:
    return GroqClient(model=model)


# ---------------------------------------------------------------------------
# Production composition
# ---------------------------------------------------------------------------

def build_production_orchestrator() -> DealHuntOrchestrator:
    """Production wiring: Groq Llama 3.3 70B everywhere; Browserbase sessions."""
    llm_70b = _default_groq(_GROQ_70B)
    llm_8b = _default_groq(_GROQ_8B)

    return DealHuntOrchestrator(
        orchestrator_llm=llm_70b,
        search_planner=SearchPlanner(llm_70b),
        page_reader=PageReader(llm_70b, tools=all_tools()),
        lead_scorer=LeadScorer(llm_8b),     # 8B is plenty for 0-1 scoring
        offer_extractor=OfferExtractor(llm_70b),
        validator=Validator(llm_70b),
        session_factory=lambda: BrowserbaseSession(proxies=True),
        rate_limiter=DomainRateLimiter(),
    )


# ---------------------------------------------------------------------------
# Eval composition — model-per-role injection
# ---------------------------------------------------------------------------

def build_eval_orchestrator(
    *,
    orchestrator_llm: LLMClient,
    search_planner_llm: LLMClient | None = None,
    page_reader_llm: LLMClient | None = None,
    lead_scorer_llm: LLMClient | None = None,
    offer_extractor_llm: LLMClient | None = None,
    validator_llm: LLMClient | None = None,
    session_factory: SessionFactory | None = None,
    rate_limiter: DomainRateLimiter | None = None,
) -> DealHuntOrchestrator:
    """Eval-suite wiring: every role can take a distinct LLMClient.

    Unspecified roles fall back to `orchestrator_llm`. Unspecified session
    factory defaults to LocalPlaywrightSession (no Browserbase credits burned).
    """
    return DealHuntOrchestrator(
        orchestrator_llm=orchestrator_llm,
        search_planner=SearchPlanner(search_planner_llm or orchestrator_llm),
        page_reader=PageReader(
            page_reader_llm or orchestrator_llm,
            tools=all_tools(),
        ),
        lead_scorer=LeadScorer(lead_scorer_llm or orchestrator_llm),
        offer_extractor=OfferExtractor(offer_extractor_llm or orchestrator_llm),
        validator=Validator(validator_llm or orchestrator_llm),
        session_factory=session_factory or (lambda: LocalPlaywrightSession()),
        rate_limiter=rate_limiter,
    )


# ---------------------------------------------------------------------------
# Backend-selecting factory — used by the Celery task in Phase 1.6
# ---------------------------------------------------------------------------

def build_orchestrator_from_env() -> DealHuntOrchestrator:
    """Reads AGENT_BROWSER_BACKEND from env, picks the right composition.

    Used by the Celery `research_for_agent` task (Phase 1.6) so production
    swaps to Browserbase automatically without code changes.
    """
    backend = os.environ.get("AGENT_BROWSER_BACKEND", "browserbase").lower()
    if backend == "local":
        llm_70b = _default_groq(_GROQ_70B)
        return build_eval_orchestrator(
            orchestrator_llm=llm_70b,
            session_factory=lambda: LocalPlaywrightSession(),
        )
    return build_production_orchestrator()
