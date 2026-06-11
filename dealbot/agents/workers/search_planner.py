"""SearchPlanner — seeds the frontier with 2-4 starting leads."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from dealbot.agents.prompts import SEARCH_PLANNER_SYSTEM, render_spec_summary
from dealbot.agents.state import Thread
from dealbot.agents.workers._json_helpers import call_with_json_output
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext


class _LeadJSON(BaseModel):
    intent: str
    url: str


class _PlanJSON(BaseModel):
    leads: list[_LeadJSON]


class SearchPlanner:
    """Single LLM call. Input: WatchlistContext + (optional) prior findings.
    Output: list[Thread] (the orchestrator pushes onto its frontier)."""

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def plan(
        self,
        spec: WatchlistContext,
        prior_findings: list[str] | None = None,
    ) -> list[Thread]:
        prior = (
            f"\n\nYou have already observed the following from earlier exploration: "
            f"{'; '.join(prior_findings[:5])}"
            if prior_findings else ""
        )
        user = (
            f"User's spec: {render_spec_summary(spec)}{prior}\n\n"
            "Return 2-4 starting leads as JSON."
        )

        parsed = await call_with_json_output(
            self.llm, SEARCH_PLANNER_SYSTEM, user, _PlanJSON,
        )

        return [
            Thread(
                id=str(uuid.uuid4()),
                intent=lead.intent,
                current_url=lead.url,
                depth=0,
            )
            for lead in parsed.leads
        ]
