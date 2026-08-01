from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)

# Backend-derived: Titan V2 is 1024-d, OpenAI text-embedding-3-small is
# 1536-d. Read at import time — the process must start with the backend it
# will write with, and mixed-dimension writes fail loudly at pgvector.
EMBED_DIM = 1024 if os.environ.get("EMBEDDING_BACKEND") == "bedrock" else 1536


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """Sequential fallback for backends without a batch endpoint.
        Returns one vector per input; [] for any that failed."""
        return [await self.embed(t) for t in texts]


class OpenAIEmbeddingClient(EmbeddingClient):
    """OpenAI text-embedding-3-small — 1536 dims, ~$0.02/million tokens."""

    _MODEL = "text-embedding-3-small"

    def __init__(self) -> None:
        self._api_key = os.environ.get("OPENAI_API_KEY", "")

    async def embed(self, text: str) -> list[float]:
        if not self._api_key:
            logger.warning("embed_text: OPENAI_API_KEY not set")
            return []
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._MODEL, "input": text},
                )
                resp.raise_for_status()
                return resp.json()["data"][0]["embedding"]
        except Exception:
            logger.warning("embed_text: OpenAI embedding failed")
            return []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        """One request for N inputs — the endpoint accepts a list for `input`.
        A hunt persists ~100 listings at once; sequential calls would be 100
        round trips."""
        if not texts:
            return []
        if not self._api_key:
            logger.warning("embed_texts: OPENAI_API_KEY not set")
            return [[] for _ in texts]
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    "https://api.openai.com/v1/embeddings",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json={"model": self._MODEL, "input": texts},
                )
                resp.raise_for_status()
                rows = sorted(resp.json()["data"], key=lambda r: r["index"])
                return [r["embedding"] for r in rows]
        except Exception:
            logger.warning("embed_texts: OpenAI batch embedding failed")
            return [[] for _ in texts]


class OllamaEmbeddingClient(EmbeddingClient):
    """Ollama nomic-embed-text — 768 dims. Only compatible if pgvector column is Vector(768)."""

    _MODEL = "nomic-embed-text"

    async def embed(self, text: str) -> list[float]:
        try:
            import ollama
            host = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
            client = ollama.AsyncClient(host=host)
            response = await client.embed(model=self._MODEL, input=text)
            return list(response.embeddings[0])
        except Exception:
            logger.warning("embed_text: Ollama embedding failed")
            return []


def _get_client() -> EmbeddingClient:
    backend = os.environ.get("EMBEDDING_BACKEND", "openai")
    if backend == "bedrock":
        # Titan V2, 1024-dim. Selecting this before migration 0025 fails
        # loudly at the pgvector write (EMBED_DIM is still 1536) — intended.
        from dealbot.llm.bedrock_client import BedrockEmbeddingClient
        return BedrockEmbeddingClient()
    if backend == "ollama":
        return OllamaEmbeddingClient()
    return OpenAIEmbeddingClient()


async def embed_text(text: str) -> list[float]:
    """Embed text using the configured backend. Returns [] on failure."""
    if not text.strip():
        return []
    return await _get_client().embed(text)


async def embed_texts(texts: list[str]) -> list[list[float]]:
    """Batch-embed. Blank inputs yield [] without an API call; the returned
    list always has one entry per input, positionally aligned."""
    if not texts:
        return []
    client = _get_client()
    idx = [i for i, t in enumerate(texts) if t.strip()]
    if not idx:
        return [[] for _ in texts]
    vectors = await client.embed_many([texts[i] for i in idx])
    out: list[list[float]] = [[] for _ in texts]
    for slot, vec in zip(idx, vectors):
        out[slot] = vec
    return out
