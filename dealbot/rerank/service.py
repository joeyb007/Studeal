"""RerankService — Cohere Rerank v3.5 wrapper.

Two-stage retrieval:
    1. SQL candidate-gen (in the caller): pull recent listings for a watchlist,
       filtered by price ceiling + marketplace subset.
    2. Cross-encoder rerank (here): score (spec_text, listing_text) pairs via
       Cohere's hosted rerank endpoint. Return top-N by relevance.

Direct httpx call — no cohere SDK dep needed. Zero-cost setup: single API key
via COHERE_API_KEY env var.

Design decisions:
    - Rerank service is deliberately narrow: it takes plain strings, returns
      indices + scores. Business logic (candidate-gen, formatting listings for
      the rerank prompt, filtering by score threshold) lives in the caller.
    - No local fallback. If Cohere is down or the key is missing, rerank
      returns candidates unchanged (identity fallback) — better than crashing
      the whole hunt endpoint.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Sequence

import httpx

logger = logging.getLogger(__name__)


_COHERE_URL = "https://api.cohere.com/v2/rerank"
_DEFAULT_MODEL = "rerank-v3.5"
_HTTP_TIMEOUT_S = 15.0


@dataclass
class RerankResult:
    """One entry in a rerank response."""
    index: int          # position in the original documents list
    relevance_score: float


class RerankService:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = _DEFAULT_MODEL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("COHERE_API_KEY", "")
        self.model = model
        self._http = http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S)

    async def rerank(
        self,
        query: str,
        documents: Sequence[str],
        top_n: int = 20,
    ) -> list[RerankResult]:
        """Return the top-N documents scored against the query.

        Identity fallback: on API failure or missing key, returns the first
        min(top_n, len(documents)) documents in original order with score 0.
        The caller can distinguish real from fallback by checking scores > 0.
        """
        if not documents:
            return []
        if not self.api_key:
            logger.warning("RerankService: no COHERE_API_KEY; returning identity")
            return _identity_fallback(len(documents), top_n)

        payload = {
            "model": self.model,
            "query": query,
            "documents": list(documents),
            "top_n": min(top_n, len(documents)),
        }
        try:
            response = await self._http.post(
                _COHERE_URL,
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            logger.warning("RerankService: Cohere call failed: %s", exc)
            return _identity_fallback(len(documents), top_n)

        results: list[RerankResult] = []
        for row in data.get("results", []):
            idx = row.get("index")
            score = row.get("relevance_score")
            if isinstance(idx, int) and isinstance(score, (int, float)):
                results.append(RerankResult(index=idx, relevance_score=float(score)))
        return results

    async def aclose(self) -> None:
        await self._http.aclose()


def _identity_fallback(doc_count: int, top_n: int) -> list[RerankResult]:
    return [
        RerankResult(index=i, relevance_score=0.0)
        for i in range(min(top_n, doc_count))
    ]
