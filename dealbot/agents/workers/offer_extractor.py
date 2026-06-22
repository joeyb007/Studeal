"""OfferExtractor — single LLM call. Converts a thread's findings into
structured DealOffer records. Enforces the observation-only rule for prices
and URLs (Validator double-checks at the end)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

from dealbot.agents.prompts import OFFER_EXTRACTOR_SYSTEM, render_spec_summary
from dealbot.agents.state import DealOffer, Provenance, Thread
from dealbot.agents.workers._json_helpers import call_with_json_output
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext


class _OfferJSON(BaseModel):
    title: str
    price: float
    price_provenance: Provenance
    listed_price: float | None = None
    listed_price_provenance: Provenance | None = None
    url: str
    url_provenance: Provenance
    retailer: str
    condition: Literal["new", "refurbished", "used", "unknown"] = "unknown"


class _OffersJSON(BaseModel):
    offers: list[_OfferJSON]


class OfferExtractor:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    async def extract(
        self,
        thread: Thread,
        spec: WatchlistContext,
        exclude_urls: list[str] | None = None,
    ) -> list[DealOffer]:
        if not thread.findings:
            return []

        findings_str = "\n".join(
            f"- [{i}] [{f.provenance}] {f.text}"
            + (f" (source: {f.source_url})" if f.source_url else "")
            for i, f in enumerate(thread.findings)
        )

        exclude_block = ""
        if exclude_urls:
            recent = exclude_urls[-20:]  # cap prompt size
            exclude_block = (
                "\n\nThe following URLs have ALREADY been extracted in a "
                "prior call. Do NOT produce offers for them — find DIFFERENT "
                "listings in the findings:\n"
                + "\n".join(f"  - {u}" for u in recent)
            )

        user = (
            f"User's spec: {render_spec_summary(spec)}\n\n"
            f"Findings collected from thread '{thread.intent}':\n"
            f"{findings_str}"
            f"{exclude_block}\n\n"
            "Extract DealOffer JSON. Skip anything you'd have to infer. "
            "Required: price_provenance MUST be 'observation', url_provenance MUST be 'observation'."
        )

        parsed = await call_with_json_output(
            self.llm, OFFER_EXTRACTOR_SYSTEM, user, _OffersJSON,
        )

        # Hard filter: drop anything that violates the provenance rule,
        # regardless of what the LLM said. Defense in depth.
        exclude_set = set(exclude_urls or [])
        result: list[DealOffer] = []
        for o in parsed.offers:
            if o.price_provenance != "observation":
                continue
            if o.url_provenance != "observation":
                continue
            if o.url in exclude_set:
                continue  # defense in depth — LLM may have ignored the exclude hint
            result.append(DealOffer(
                title=o.title,
                price=o.price,
                price_provenance=o.price_provenance,
                listed_price=o.listed_price,
                listed_price_provenance=o.listed_price_provenance,
                url=o.url,
                url_provenance=o.url_provenance,
                retailer=o.retailer,
                condition=o.condition,
            ))
        return result
