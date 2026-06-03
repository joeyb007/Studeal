from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, HttpUrl


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
    url: Optional[str] = None
    listed_price: float
    sale_price: float
    asin: Optional[str] = None
    description: Optional[str] = None
    student_eligible: bool = False  # True if page content confirms student pricing/discount
    condition: Condition = Condition.unknown
    source_type: str = "scraped"             # "api" | "scraped" — api results skip re-extraction
    # URL resolution identity — set at extraction time so find_url needs no lookup dict
    raw_button_label: Optional[str] = None  # exact UI button string from the DOM
    listing_index: Optional[int] = None     # 1-based position in organic listing section
    search_query: Optional[str] = None      # Google Shopping query that produced this listing


class ValidationResult(BaseModel):
    """Output of the validation layer. Decides deal legitimacy; ranking is by cosine similarity."""

    deal: DealRaw
    legitimate: bool
    validation_confidence: float  # 0.0 - 1.0
    validation_reason: str
    category: Category = Category.other
    condition: Condition = Condition.unknown
    student_eligible: bool = False
    real_discount_pct: Optional[float] = None
    tags: list[str] = []


class WatchlistContext(BaseModel):
    product_query: str
    max_budget: Optional[float] = None
    min_discount_pct: Optional[int] = None
    condition: list[str] = []
    brands: list[str] = []
    keywords: list[str] = []


class ChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class TurnResult(BaseModel):
    reply: str
    context: WatchlistContext
    is_complete: bool
    suggestions: list[str] = []
    turns_remaining: int = 0
    aborted: bool = False
    abort_reason: Optional[str] = None
    abort_code: Optional[str] = None  # off_topic | adversarial | unintelligible | non_shopping


class WatchlistContextPatch(BaseModel):
    max_budget: Optional[float] = None
    min_discount_pct: Optional[int] = None
    condition: Optional[list[str]] = None
    brands: Optional[list[str]] = None

