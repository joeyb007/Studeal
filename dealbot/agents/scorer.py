from __future__ import annotations

import json
import logging

from dealbot.llm.base import LLMClient
from dealbot.schemas import Category, Condition, DealRaw, ValidationResult

logger = logging.getLogger(__name__)


_VALIDATION_PROMPT = """\
You are a deal validation agent. Your job is to decide whether a deal is REAL, \
LEGITIMATE, and SAFE TO SURFACE to users — not to score how good it is.

Reject deals (legitimate=false) when ANY of these apply:
- Price is implausibly low for the product (likely auction-start, scam, or pricing error)
  Example: AirPods Max at $19.99, iPhone 16 at $80, MacBook Pro at $200
- Title indicates parts only, broken, "for repair", "as is", "not working"
- Title suggests counterfeit, fake, replica, knockoff
- Title is missing the actual product name (just "Apple Headphones" with no model)
- Listing is clearly an accessory mistakenly indexed as the product itself \
  (e.g. "AirPods Max Case" priced at $25 when looking for AirPods Max)
- Suspicious seller signals in the title ("Hot Dog Vendor", random numbers, gibberish)

Accept deals (legitimate=true) when:
- Price is plausible for the product (use your training knowledge of typical \
  Canadian retail prices — products in CAD typically cost ~30% more than USD)
- Title clearly identifies a real product
- Condition matches the price reasonably (a refurb at 40% off MSRP is normal; \
  refurb at 95% off is suspicious)
- Even full-price listings from major retailers are legitimate (we'll let users \
  filter by discount % at view time — that's not your concern)

Also extract these fields based on the listing:
- category: one of "Electronics" | "Laptops" | "Tablets" | "Phones" | "Audio" | \
  "Gaming" | "Accessories" | "Software" | "Books" | "Clothing" | "Food & Drink" | \
  "Travel" | "Home" | "Other"
- condition: "new" | "used" | "refurb" | "unknown"
- student_eligible: true ONLY if the listing explicitly mentions student pricing/discount
- real_discount_pct: computed as round((listed - sale) / listed * 100) when listed > sale, \
  otherwise null
- tags: short labels like ["bestbuy", "open-box", "limited-time"]

confidence is your certainty in the legitimacy decision (0.0–1.0). \
High confidence on obvious cases, lower on ambiguous ones.

Respond with ONLY a JSON object, no other text:
{
  "legitimate": <true|false>,
  "validation_confidence": <float 0.0-1.0>,
  "validation_reason": "<one short sentence explaining the decision>",
  "category": "<category>",
  "condition": "<condition>",
  "student_eligible": <true|false>,
  "real_discount_pct": <float or null>,
  "tags": [<short strings>]
}"""


def _deal_to_text(deal: DealRaw) -> str:
    lines = [
        f"Title: {deal.title}",
        f"Source: {deal.source}",
        f"Listed price: ${deal.listed_price}",
        f"Sale price: ${deal.sale_price}",
        f"URL: {deal.url}",
    ]
    if deal.description:
        lines.append(f"Description: {deal.description[:300]}")
    return "\n".join(lines)


def _fallback_rejection(deal: DealRaw, reason: str) -> ValidationResult:
    """Emitted when validation fails — defaults to rejection so we don't surface unknowns."""
    return ValidationResult(
        deal=deal,
        legitimate=False,
        validation_confidence=0.0,
        validation_reason=reason,
        category=Category.other,
        condition=Condition.unknown,
        student_eligible=False,
        real_discount_pct=None,
        tags=[],
    )


class ScorerAgent:
    """Validation layer (formerly scoring). One LLM call, no tools, no RAG."""

    def __init__(self, llm: LLMClient) -> None:
        self._llm = llm

    async def validate(self, deal: DealRaw) -> ValidationResult:
        messages = [
            {"role": "system", "content": _VALIDATION_PROMPT},
            {"role": "user", "content": _deal_to_text(deal)},
        ]

        try:
            response = await self._llm.complete(
                messages,
                response_format={"type": "json_object"},
            )
        except Exception as exc:
            logger.warning("ScorerAgent: LLM call failed: %s", exc)
            return _fallback_rejection(deal, f"LLM error: {exc}")

        return self._parse(response.content, deal)

    def _parse(self, content: str | None, deal: DealRaw) -> ValidationResult:
        if not content:
            return _fallback_rejection(deal, "empty LLM response")

        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.removeprefix("```json").removeprefix("```").removesuffix("```").strip()

        try:
            data = json.loads(cleaned)
            return ValidationResult(deal=deal, **data)
        except Exception:
            logger.warning("ScorerAgent: failed to parse response: %r", content[:300])
            return _fallback_rejection(deal, "failed to parse LLM response")

    # Back-compat shim — old callers using .score() get rejected loudly so we catch them
    async def score(self, *args, **kwargs):  # pragma: no cover
        raise RuntimeError(
            "ScorerAgent.score() is removed. Use ScorerAgent.validate(deal) which returns ValidationResult."
        )
