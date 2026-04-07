from __future__ import annotations

import logging

import ollama

logger = logging.getLogger(__name__)

EMBED_MODEL = "nomic-embed-text"
EMBED_DIM = 768


async def embed_text(text: str) -> list[float]:
    """Generate a 768-dim embedding via Ollama nomic-embed-text.

    Returns an empty list if Ollama is unreachable or the model is missing,
    so callers can degrade gracefully rather than crash.
    """
    if not text.strip():
        return []
    try:
        client = ollama.AsyncClient()
        response = await client.embed(model=EMBED_MODEL, input=text)
        return list(response.embeddings[0])
    except Exception:
        logger.warning("embed_text: failed to embed text (Ollama unavailable?)")
        return []
