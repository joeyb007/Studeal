from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

# vLLM serves an OpenAI-compatible API at /v1/chat/completions.
# Point VLLM_BASE_URL at your cloud-hosted vLLM instance in prod.
# Locally this can also point at a local vLLM server for testing.
_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_MODEL = "meta-llama/Llama-3.1-8B-Instruct"


class vLLMClient(LLMClient):
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("VLLM_MODEL", _DEFAULT_MODEL)
        self.base_url = (base_url or os.environ.get("VLLM_BASE_URL", _DEFAULT_BASE_URL)).rstrip("/")

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"

        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
            )
            resp.raise_for_status()
            data = resp.json()

        message = data["choices"][0]["message"]
        content: str | None = message.get("content")
        tool_calls: list[ToolCall] = []

        for tc in message.get("tool_calls") or []:
            arguments = tc["function"]["arguments"]
            # vLLM may return arguments as a string or already-parsed dict
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id", tc["function"]["name"]),
                    name=tc["function"]["name"],
                    arguments=arguments,
                )
            )

        return LLMResponse(content=content, tool_calls=tool_calls)
