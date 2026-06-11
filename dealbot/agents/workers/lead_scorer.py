"""LeadScorer — single LLM call, returns a float 0-1 estimating
information gain for a lead given the current state of exploration."""

from __future__ import annotations

from pydantic import BaseModel, Field

from dealbot.agents.prompts import LEAD_SCORER_SYSTEM, render_spec_summary
from dealbot.agents.state import OrchestratorState, Thread
from dealbot.agents.workers._json_helpers import call_with_json_output
from dealbot.llm.base import LLMClient


class _ScoreJSON(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    reasoning: str


class LeadScorer:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def score(self, lead: Thread, state: OrchestratorState) -> float:
        # Build a compact state summary: visited domains + current offer count.
        visited_domains: set[str] = set()
        for thread in [state.current_thread] + state.frontier + state.parked:
            if thread is None:
                continue
            for u in thread.visited_urls:
                d = _domain(u)
                if d:
                    visited_domains.add(d)

        lead_domain = _domain(lead.current_url or "")

        user = (
            f"User's spec: {render_spec_summary(state.spec)}\n\n"
            f"New lead intent: {lead.intent!r}\n"
            f"New lead URL: {lead.current_url!r}\n"
            f"New lead's domain: {lead_domain!r}\n\n"
            f"Already-visited domains: {sorted(visited_domains) or 'none yet'}\n"
            f"Current offer count: {len(state.offers)}\n"
            f"Current turn: {state.turn}\n\n"
            "Score this lead 0-1."
        )

        parsed = await call_with_json_output(
            self.llm, LEAD_SCORER_SYSTEM, user, _ScoreJSON,
        )
        return parsed.score


def _domain(url: str) -> str:
    try:
        from urllib.parse import urlparse
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""
