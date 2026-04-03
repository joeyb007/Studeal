from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

# Models that support OpenAI-spec native tool calling via Ollama
SUPPORTS_NATIVE_TOOLS: set[str] = {
    "llama3.1",
    "llama3.1:8b",
    "llama3.1:70b",
    "mistral-nemo",
}

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "llama3.1": {"supports_native_tools": True, "context_window": 128_000},
    "llama3.1:8b": {"supports_native_tools": True, "context_window": 128_000},
    "mistral-nemo": {"supports_native_tools": True, "context_window": 128_000},
}


class OllamaClient(LLMClient):
    def __init__(
        self,
        model: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("OLLAMA_MODEL", "llama3.1")
        self.base_url = base_url or os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
        self._native_tools = self.model in SUPPORTS_NATIVE_TOOLS

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
            if self._native_tools:
                payload["tools"] = tools
            else:
                # Inject tools as JSON schema into the last system message
                tool_schema = json.dumps(tools, indent=2)
                inject = {
                    "role": "system",
                    "content": (
                        "You have access to the following tools. "
                        "To call a tool, respond with a JSON object with keys "
                        '"tool" and "arguments". Do not add any other text.\n\n'
                        f"{tool_schema}"
                    ),
                }
                payload["messages"] = [inject] + messages

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(f"{self.base_url}/api/chat", json=payload)
            resp.raise_for_status()
            data = resp.json()

        message = data["message"]
        tool_calls: list[ToolCall] = []

        if self._native_tools and message.get("tool_calls"):
            for tc in message["tool_calls"]:
                tool_calls.append(
                    ToolCall(
                        id=tc.get("id", tc["function"]["name"]),
                        name=tc["function"]["name"],
                        arguments=tc["function"]["arguments"],
                    )
                )
        elif not self._native_tools and message.get("content"):
            # Try to parse manual JSON tool call from content
            try:
                parsed = json.loads(message["content"])
                if "tool" in parsed:
                    tool_calls.append(
                        ToolCall(
                            id=parsed["tool"],
                            name=parsed["tool"],
                            arguments=parsed.get("arguments", {}),
                        )
                    )
            except (json.JSONDecodeError, KeyError):
                pass

        return LLMResponse(
            content=message.get("content"),
            tool_calls=tool_calls,
        )
