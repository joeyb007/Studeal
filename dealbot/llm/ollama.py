from __future__ import annotations

import json
import os
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

# Models confirmed to support OpenAI-spec native tool calling via Ollama.
# Source: https://ollama.com/search?c=tools (verified April 2026)
SUPPORTS_NATIVE_TOOLS: set[str] = {
    # Llama 3.x family
    "llama3.1",
    "llama3.1:8b",
    "llama3.1:70b",
    "llama3.2",
    "llama3.2:1b",
    "llama3.2:3b",
    "llama3.3",
    "llama3.3:70b",
    # Mistral family
    "mistral-nemo",
    "mistral-small",
    "mistral-small:22b",
    # Qwen3 family
    "qwen3",
    "qwen3:0.6b",
    "qwen3:1.7b",
    "qwen3:4b",
    "qwen3:8b",
    "qwen3:14b",
    "qwen3:32b",
    # Phi-4
    "phi4-mini",
    "phi4-mini:3.8b",
    # IBM Granite
    "granite3-dense",
    "granite3-dense:2b",
    "granite3-dense:8b",
    "granite3-moe",
}

MODEL_REGISTRY: dict[str, dict[str, Any]] = {
    "llama3.1":         {"supports_native_tools": True, "context_window": 128_000},
    "llama3.1:8b":      {"supports_native_tools": True, "context_window": 128_000},
    "llama3.1:70b":     {"supports_native_tools": True, "context_window": 128_000},
    "llama3.2":         {"supports_native_tools": True, "context_window": 128_000},
    "llama3.3":         {"supports_native_tools": True, "context_window": 128_000},
    "mistral-nemo":     {"supports_native_tools": True, "context_window": 128_000},
    "mistral-small":    {"supports_native_tools": True, "context_window": 128_000},
    "qwen3":            {"supports_native_tools": True, "context_window": 128_000},
    "qwen3:8b":         {"supports_native_tools": True, "context_window": 128_000},
    "phi4-mini":        {"supports_native_tools": True, "context_window": 16_000},
    "granite3-dense":   {"supports_native_tools": True, "context_window": 128_000},
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
