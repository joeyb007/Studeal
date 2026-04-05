from __future__ import annotations

import os
from typing import Any

import anthropic

from .base import LLMClient, LLMResponse, ToolCall

_DEFAULT_MODEL = "claude-sonnet-4-6"


class AnthropicClient(LLMClient):
    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
    ) -> None:
        self.model = model or os.environ.get("ANTHROPIC_MODEL", _DEFAULT_MODEL)
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or os.environ.get("ANTHROPIC_API_KEY")
        )

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        # Anthropic separates the system prompt from the messages array
        system_prompt = ""
        filtered: list[dict[str, Any]] = []
        for msg in messages:
            if msg["role"] == "system":
                system_prompt += msg["content"] + "\n"
            else:
                filtered.append(msg)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 1024,
            "messages": filtered,
        }
        if system_prompt:
            kwargs["system"] = system_prompt.strip()

        # Convert OpenAI-style tool schema to Anthropic tool schema
        if tools:
            kwargs["tools"] = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"].get("description", ""),
                    "input_schema": t["function"]["parameters"],
                }
                for t in tools
            ]

        response = await self._client.messages.create(**kwargs)

        content_text: str | None = None
        tool_calls: list[ToolCall] = []

        for block in response.content:
            if block.type == "text":
                content_text = block.text
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block.id,
                        name=block.name,
                        arguments=block.input,
                    )
                )

        return LLMResponse(content=content_text, tool_calls=tool_calls)
