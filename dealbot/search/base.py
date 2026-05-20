from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class SearchResult:
    """Provider-agnostic normalized search result."""

    title: str
    url: str
    snippet: str = ""
    sale_price: float | None = None
    listed_price: float | None = None
    source_domain: str = ""
    image_url: str | None = None
    provider: str = ""
    raw: dict = field(default_factory=dict)


class SearchProvider(ABC):
    """Abstract search provider — every concrete provider normalizes to SearchResult."""

    name: str = "unknown"
    cost_per_query_usd: float = 0.0

    @abstractmethod
    async def search(self, query: str, locale: str = "ca") -> list[SearchResult]:
        ...

    def is_configured(self) -> bool:
        return True
