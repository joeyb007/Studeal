from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class SearchResult(BaseModel):
    """A single result returned by the search API."""

    title: str
    url: str
    description: str
    age: str | None = None


class FetchedPage(BaseModel):
    """Cleaned text content fetched from a search result URL."""

    url: str
    text: str  # scripts/styles/nav stripped, truncated to 4000 chars
    links: list[str] = []  # absolute hrefs extracted from <a> tags


class SearchClient(ABC):
    @abstractmethod
    async def search(self, query: str, n: int) -> list[SearchResult]: ...
