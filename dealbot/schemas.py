from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, HttpUrl


class AlertTier(str, Enum):
    none = "none"
    digest = "digest"
    push = "push"


class Condition(str, Enum):
    new = "new"
    used = "used"
    refurb = "refurb"
    unknown = "unknown"


class Category(str, Enum):
    electronics = "Electronics"
    laptops = "Laptops"
    tablets = "Tablets"
    phones = "Phones"
    audio = "Audio"
    gaming = "Gaming"
    accessories = "Accessories"
    software = "Software"
    books = "Books"
    clothing = "Clothing"
    food_drink = "Food & Drink"
    travel = "Travel"
    home = "Home"
    other = "Other"


class DealRaw(BaseModel):
    """Normalised deal data produced by the ScraperAgent."""

    source: str
    title: str
    url: str
    listed_price: float
    sale_price: float
    asin: Optional[str] = None
    description: Optional[str] = None
    student_eligible: bool = False  # True if page content confirms student pricing/discount
    condition: Condition = Condition.unknown
    # URL resolution identity — set at extraction time so find_url needs no lookup dict
    raw_button_label: Optional[str] = None  # exact UI button string from the DOM
    listing_index: Optional[int] = None     # 1-based position in organic listing section
    search_query: Optional[str] = None      # Google Shopping query that produced this listing


class DealScore(BaseModel):
    """Scored deal produced by the ScorerAgent."""

    deal: DealRaw
    score: int  # 0-100
    alert_tier: AlertTier
    category: Category
    tags: list[str]
    real_discount_pct: Optional[float] = None
    confidence: str = "high"  # "high" | "low" (low if max iterations hit)
    condition: Condition = Condition.unknown
