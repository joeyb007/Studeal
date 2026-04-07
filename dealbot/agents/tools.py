from __future__ import annotations

from typing import Optional

from pydantic import BaseModel


# --- Return types -----------------------------------------------------------

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
            "name": "verify_discount",
            "description": (
                "Cross-reference the listed and sale prices to confirm whether "
                "the discount is genuine. Flags anything over 70% as suspicious."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "listed": {"type": "number", "description": "The listed (original) price"},
                    "sale": {"type": "number", "description": "The sale price"},
                },
                "required": ["listed", "sale"],
            },
        },
    },
]


# --- Implementations --------------------------------------------------------

async def verify_discount(
    listed: float,
    sale: float,
    asin: Optional[str] = None,
) -> DiscountVerification:
    """Compute real discount % and flag anything over 70% as suspicious."""
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
    "verify_discount": verify_discount,
}
