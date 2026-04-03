from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# --- Return types -----------------------------------------------------------

class PriceHistory(BaseModel):
    asin: str
    all_time_low: float
    avg_90_day: float
    current: float


class DiscountVerification(BaseModel):
    listed_price: float
    sale_price: float
    real_discount_pct: float
    is_genuine: bool  # False if listed_price looks inflated vs history


# --- Tool definitions (JSON schema sent to the LLM) -------------------------

TOOL_DEFINITIONS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "fetch_price_history",
            "description": (
                "Look up the price history for a product by ASIN. "
                "Returns all-time low, 90-day average, and current price."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "asin": {"type": "string", "description": "Amazon product ASIN"},
                },
                "required": ["asin"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_discount",
            "description": (
                "Cross-reference the listed and sale prices against price history "
                "to confirm whether the discount is genuine."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "listed": {"type": "number", "description": "The listed (original) price"},
                    "sale": {"type": "number", "description": "The sale price"},
                    "asin": {"type": "string", "description": "Amazon product ASIN (optional)"},
                },
                "required": ["listed", "sale"],
            },
        },
    },
]


# --- Stub implementations ---------------------------------------------------
# These return plausible fake data. Replace with real API calls in Phase 2.

async def fetch_price_history(asin: str) -> PriceHistory:
    """Stub: returns fake price history data."""
    return PriceHistory(
        asin=asin,
        all_time_low=round(float(hash(asin) % 50 + 80), 2),
        avg_90_day=round(float(hash(asin) % 80 + 150), 2),
        current=round(float(hash(asin) % 100 + 200), 2),
    )


async def verify_discount(
    listed: float,
    sale: float,
    asin: Optional[str] = None,
) -> DiscountVerification:
    """Stub: computes discount pct and flags anything over 70% as suspicious."""
    real_discount_pct = round((listed - sale) / listed * 100, 1)
    is_genuine = real_discount_pct <= 70.0
    return DiscountVerification(
        listed_price=listed,
        sale_price=sale,
        real_discount_pct=real_discount_pct,
        is_genuine=is_genuine,
    )


# --- Registry (tool name → callable) ----------------------------------------

TOOL_REGISTRY: dict[str, object] = {
    "fetch_price_history": fetch_price_history,
    "verify_discount": verify_discount,
}
