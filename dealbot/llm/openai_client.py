from __future__ import annotations

import json
import logging
import os
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"


class OpenAIClient(LLMClient):
    """OpenAI inference backend. Reliable tool calling, low cost."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("OPENAI_MODEL", _DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
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
        if response_format:
            payload["response_format"] = response_format

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_OPENAI_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )
            if not resp.is_success:
                logger.error("OpenAIClient: %s — %s", resp.status_code, resp.text)
            resp.raise_for_status()
            data = resp.json()

        choice = data["choices"][0]["message"]
        tool_calls: list[ToolCall] = []
        for tc in choice.get("tool_calls") or []:
            try:
                arguments = json.loads(tc["function"]["arguments"])
                if not isinstance(arguments, dict):
                    continue
                tool_calls.append(
                    ToolCall(
                        id=tc["id"],
                        name=tc["function"]["name"],
                        arguments=arguments,
                    )
                )
            except (json.JSONDecodeError, KeyError):
                logger.debug("OpenAIClient: skipping malformed tool call: %s", tc)

        return LLMResponse(content=choice.get("content"), tool_calls=tool_calls)
