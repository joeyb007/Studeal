from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, HttpUrl, field_validator


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


class SpecAttribute(BaseModel):
    """One elicited product attribute (attribute-spec 2026-08-11).

    tier="must" means a contradiction disqualifies the listing (hard demote);
    tier="nice" contributes to fit scoring only. Unknown tiers degrade to
    "nice" rather than failing the whole context parse.
    """

    name: str    # "handedness", "set composition", "bag"
    value: str   # "right-handed", "complete set", "included"
    tier: str = "nice"

    @field_validator("tier", mode="before")
    @classmethod
    def _known_tier(cls, v: object) -> str:
        return v if v in ("must", "nice") else "nice"


class WatchlistContext(BaseModel):
    product_query: str
    max_budget: Optional[float] = None
    min_discount_pct: Optional[int] = None
    condition: list[str] = []
    brands: list[str] = []
    keywords: list[str] = []
    # 1-2 sentences on who this buyer is and what they value. Elicited by Scout,
    # never asked for directly. Shapes discovery and ranking only — hard
    # constraints stay in the typed fields above.
    buyer_profile: Optional[str] = None
    # How picky this buyer is about cosmetic condition; drives quality
    # filtering of picks. Inferred or asked once by Scout, editable later.
    quality_bar: Optional[str] = None  # pristine | good | wear_ok | any
    # The physical brief (2026-08-10 spec): color, shape/variant, size, age
    # and wear tolerance, must-have traits, in the buyer's own words
    # ("l shaped brown sectional, mild to medium wear, under 10 years").
    appearance_notes: Optional[str] = None
    # Category-sharp facts about the right item (attribute-spec 2026-08-11):
    # composition/variant, included accessories, fitment, spec level. Elicited
    # by Scout's attribute beat; musts gate ranking, nices score fit.
    attributes: list[SpecAttribute] = []

    @field_validator("quality_bar", mode="before")
    @classmethod
    def _known_quality_bar(cls, v: object) -> Optional[str]:
        # The LLM fills this field; an unknown label degrades to "unset"
        # rather than failing the whole context parse.
        return v if v in ("pristine", "good", "wear_ok", "any") else None


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
    buyer_profile: Optional[str] = None
    quality_bar: Optional[str] = None
    appearance_notes: Optional[str] = None
    attributes: Optional[list[SpecAttribute]] = None

