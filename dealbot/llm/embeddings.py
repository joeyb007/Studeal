from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod

import httpx

logger = logging.getLogger(__name__)

EMBED_DIM = 1536


class EmbeddingClient(ABC):
    @abstractmethod
    async def embed(self, text: str) -> list[float]: ...


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
    if backend == "ollama":
        return OllamaEmbeddingClient()
    return OpenAIEmbeddingClient()


async def embed_text(text: str) -> list[float]:
    """Embed text using the configured backend. Returns [] on failure."""
    if not text.strip():
        return []
    return await _get_client().embed(text)
