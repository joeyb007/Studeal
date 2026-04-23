from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

_GROQ_BASE = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.1-70b-versatile"


class GroqClient(LLMClient):
    """Groq inference backend. OpenAI-compatible API, fast parallel calls."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("GROQ_MODEL", _DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_GROQ_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]
        tool_calls: list[ToolCall] = []
        for tc in choice.get("tool_calls") or []:
            tool_calls.append(
                ToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=json.loads(tc["function"]["arguments"]),
                )
            )

        return LLMResponse(content=choice.get("content"), tool_calls=tool_calls)
