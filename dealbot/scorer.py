from __future__ import annotations

_TRUSTED_SOURCES = {
    "amazon.ca", "amazon.com",
    "bestbuy.ca", "bestbuy.com",
    "costco.ca", "costco.com",
    "walmart.ca", "walmart.com",
    "canadacomputers.com",
    "newegg.ca", "newegg.com",
    "staples.ca",
}

_CONDITION_BONUS = {
    "new": 0,
    "refurb": 8,
    "used": 3,
    "unknown": 0,
}


def compute_deal_score(
    *,
    discount_pct: float | None,
    validation_confidence: float,
    condition: str,
    student_eligible: bool,
    source: str,
) -> int:
    """Deterministic 0-100 deal quality score.

    Weights chosen to reward real discounts heavily while using
    validation confidence, condition, and source trust as modifiers.
    """
    # Discount depth: 0-60 pts. Caps at 80% (anything higher is likely fraud).
    disc_pts = min(discount_pct or 0.0, 80.0) / 80.0 * 60.0

    # Validation confidence: 0-25 pts
    confidence_pts = validation_confidence * 25.0

    # Condition bonus: refurb deals are legitimately good value
    condition_pts = _CONDITION_BONUS.get(condition, 0)

    # Student eligibility: bonus 5 pts (narrows addressable audience)
    student_pts = 5.0 if student_eligible else 0.0

    # Source trust: known retailers reduce fraud risk
    source_lower = source.lower()
    trusted = any(s in source_lower for s in _TRUSTED_SOURCES)
    source_pts = 10.0 if trusted else 4.0

    total = disc_pts + confidence_pts + condition_pts + student_pts + source_pts
    return max(0, min(100, int(total)))
