from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, HttpUrl


class AlertTier(str, Enum):
    none = "none"
    digest = "digest"
    push = "push"


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


class DealScore(BaseModel):
    """Scored deal produced by the ScorerAgent."""

    deal: DealRaw
    score: int  # 0-100
    alert_tier: AlertTier
    category: str
    tags: list[str]
    real_discount_pct: Optional[float] = None
    confidence: str = "high"  # "high" | "low" (low if max iterations hit)
