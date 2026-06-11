"""Validator — single LLM call. Final acceptance gate. Drops offers that
violate provenance, are out of budget, or otherwise inconsistent with the
spec. Can request replan leads (the orchestrator caps replans at 1 cycle)."""

from __future__ import annotations

import uuid

from pydantic import BaseModel

from dealbot.agents.prompts import VALIDATOR_SYSTEM, render_spec_summary
from dealbot.agents.state import DealOffer, Thread
from dealbot.agents.workers._json_helpers import call_with_json_output
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext


class _SuggestedLeadJSON(BaseModel):
    intent: str
    url: str


class _ValidationJSON(BaseModel):
    acceptable: bool
    kept_offer_indices: list[int]
    feedback: str
    suggested_leads: list[_SuggestedLeadJSON] = []


class ValidationDecision(BaseModel):
    acceptable: bool
    kept_offers: list[DealOffer]
    feedback: str
    suggested_leads: list[Thread]


class Validator:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def validate(
        self, spec: WatchlistContext, offers: list[DealOffer],
    ) -> ValidationDecision:
        if not offers:
            return ValidationDecision(
                acceptable=False,
                kept_offers=[],
                feedback="No offers collected.",
                suggested_leads=[],
            )

        offers_str = "\n".join(
            f"[{i}] {o.title} @ ${o.price:.2f} ({o.retailer}, {o.condition}) — {o.url}"
            for i, o in enumerate(offers)
        )

        user = (
            f"User's spec: {render_spec_summary(spec)}\n\n"
            f"Collected offers:\n{offers_str}\n\n"
            "Decide which offers to keep + whether the set is acceptable. "
            "If not acceptable, suggest 1-2 corrective leads."
        )

        parsed = await call_with_json_output(
            self.llm, VALIDATOR_SYSTEM, user, _ValidationJSON,
        )

        kept = [offers[i] for i in parsed.kept_offer_indices if 0 <= i < len(offers)]

        suggested = [
            Thread(
                id=str(uuid.uuid4()),
                intent=lead.intent,
                current_url=lead.url,
                depth=0,
            )
            for lead in parsed.suggested_leads
        ]

        return ValidationDecision(
            acceptable=parsed.acceptable,
            kept_offers=kept,
            feedback=parsed.feedback,
            suggested_leads=suggested,
        )
