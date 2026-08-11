"""Listwise reranking — stage 3 of the recommendation pipeline.

Stage 2 (dense retrieval) is a bi-encoder: it compresses the whole query and each
listing to one vector apiece, which is fast over a large pool but cannot resolve
negation ("over-ear, NOT earbuds") or weigh fine distinctions. This stage sees the
shortlist together and orders it, emitting a reason per listing that the alert
email and the results UI both render.

Degradation, in order:
    1. Our listwise LLM ranker (primary).
    2. Cohere cross-encoder, only when RANKER_BASELINE=cohere and a key exists.
       Retained as a measurable baseline, not because we depend on it.
    3. Candidate order at score 0.0 — retrieval order is a real ranking.

Never returns empty for a non-empty candidate list. Callers threshold on score,
so a silent empty result would drop every alert.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Sequence

from dealbot.db.models import Listing
from dealbot.llm.base import LLMClient
from dealbot.schemas import WatchlistContext

logger = logging.getLogger(__name__)

# Candidates per LLM call. Ranking quality degrades sharply on long lists — the
# same lesson chunked extraction already learned on page snapshots.
RANK_WINDOW = 25


@dataclass
class RankedListing:
    listing: Listing
    score: float
    reason: str


_SYSTEM = """You rank marketplace listings for one buyer. You are given their \
spec and a numbered list of listings. Return every listing, ordered best first.

Score 0.0-1.0 on how well the listing serves THIS buyer, not on how good a deal \
it is in the abstract. A cheap item that is the wrong thing scores low.

The reason is one short sentence a buyer would find useful — what makes this \
listing fit or not fit them specifically. Never restate the price alone.

Respond ONLY with valid JSON:
{"rankings": [{"index": 0, "score": 0.9, "reason": "..."}, ...]}"""


def _spec_text(spec: WatchlistContext) -> str:
    parts = [f"Looking for: {spec.product_query}"]
    if spec.buyer_profile and spec.buyer_profile.strip():
        parts.append(f"About the buyer: {spec.buyer_profile.strip()}")
    if spec.appearance_notes and spec.appearance_notes.strip():
        parts.append(
            f"Physical brief (what it should look like): {spec.appearance_notes.strip()}. "
            "A listing that clearly contradicts the brief serves this buyer poorly, "
            "however good the deal."
        )
    musts = [a.value.strip() for a in spec.attributes
             if a.tier == "must" and a.value.strip()]
    nices = [a.value.strip() for a in spec.attributes
             if a.tier == "nice" and a.value.strip()]
    if musts:
        parts.append(
            f"Requirements (non-negotiable): {'; '.join(musts)}. A listing that "
            "contradicts one of these is the wrong item — score it below every "
            "non-contradicting listing, whatever the price. A listing that "
            "doesn't say either way is NOT a contradiction."
        )
    if nices:
        parts.append(
            f"Nice to have: {'; '.join(nices)}. These raise fit; they never "
            "disqualify."
        )
    if spec.brands:
        parts.append(f"Prefers brands: {', '.join(spec.brands)}")
    if spec.condition:
        parts.append(f"Acceptable condition: {', '.join(spec.condition)}")
    # max_budget is deliberately absent: the ceiling is enforced as a SQL filter
    # upstream. Telling the ranker about a constraint it must not be able to
    # relax is an invitation to relax it.
    return "\n".join(parts)


def _listing_text(listing: Listing) -> str:
    parts = [listing.title, f"${listing.price:.2f} {listing.currency}", listing.marketplace]
    if listing.location:
        parts.append(listing.location)
    if listing.condition and listing.condition != "unknown":
        parts.append(f"condition: {listing.condition}")
    return " | ".join(parts)


def _unranked(candidates: Sequence[Listing], top_n: int) -> list[RankedListing]:
    return [RankedListing(listing=c, score=0.0, reason="") for c in candidates[:top_n]]


async def _rank_window(
    spec_text: str, window: Sequence[Listing], llm: LLMClient,
) -> list[RankedListing]:
    """Rank one window. Returns [] on any failure — the caller decides policy."""
    listing_block = "\n".join(f"[{i}] {_listing_text(c)}" for i, c in enumerate(window))
    messages = [
        {"role": "system", "content": _SYSTEM},
        {"role": "user", "content": f"{spec_text}\n\nListings:\n{listing_block}"},
    ]
    try:
        response = await llm.complete(messages, response_format={"type": "json_object"})
        rows = json.loads(response.content or "{}").get("rankings", [])
    except Exception:
        logger.warning("rank: window failed", exc_info=True)
        return []

    ranked: list[RankedListing] = []
    seen: set[int] = set()
    for row in rows:
        index = row.get("index")
        # A hallucinated index must not raise or fabricate a listing.
        if not isinstance(index, int) or not (0 <= index < len(window)) or index in seen:
            continue
        seen.add(index)
        score = row.get("score")
        ranked.append(RankedListing(
            listing=window[index],
            score=float(score) if isinstance(score, (int, float)) else 0.0,
            reason=str(row.get("reason") or ""),
        ))
    # Anything the model omitted still belongs in the result — dropping a listing
    # because the ranker forgot it is a silent recall loss.
    for i, listing in enumerate(window):
        if i not in seen:
            ranked.append(RankedListing(listing=listing, score=0.0, reason=""))
    return ranked


async def _cohere_baseline(
    spec_text: str, candidates: Sequence[Listing], top_n: int,
) -> list[RankedListing]:
    from dealbot.rerank.service import RerankService

    service = RerankService()
    try:
        results = await service.rerank(
            spec_text, [_listing_text(c) for c in candidates], top_n=top_n,
        )
    finally:
        await service.aclose()
    # All-zero scores means Cohere fell back to identity (no key / API down),
    # which carries no ranking information at all.
    if not any(r.relevance_score > 0 for r in results):
        return []
    return [
        RankedListing(listing=candidates[r.index], score=r.relevance_score, reason="")
        for r in results
        if 0 <= r.index < len(candidates)
    ]


def _default_llm() -> LLMClient:
    from dealbot.llm.openai_client import OpenAIClient

    return OpenAIClient()


async def rank(
    spec: WatchlistContext,
    candidates: Sequence[Listing],
    *,
    llm: LLMClient | None = None,
    top_n: int = 20,
) -> list[RankedListing]:
    """Order candidates for this buyer, best first, with a reason each.

    Hard constraints must already have been applied as SQL filters upstream —
    this stage cannot enforce them and is not told about them.
    """
    if not candidates:
        return []

    spec_text = _spec_text(spec)
    llm = llm or _default_llm()

    windows = [
        candidates[i:i + RANK_WINDOW]
        for i in range(0, len(candidates), RANK_WINDOW)
    ]
    results = await asyncio.gather(
        *(_rank_window(spec_text, w, llm) for w in windows),
        return_exceptions=True,
    )

    merged: list[RankedListing] = []
    failed_windows = 0
    for window, result in zip(windows, results):
        if isinstance(result, BaseException) or not result:
            failed_windows += 1
            # Keep the window's listings at score 0 rather than losing them.
            merged.extend(_unranked(window, len(window)))
            continue
        merged.extend(result)

    if failed_windows == len(windows):
        logger.warning("rank: every window failed; trying baseline")
        if os.environ.get("RANKER_BASELINE") == "cohere":
            baseline = await _cohere_baseline(spec_text, candidates, top_n)
            if baseline:
                return baseline
        return _unranked(candidates, top_n)

    merged.sort(key=lambda r: r.score, reverse=True)
    return merged[:top_n]
