from __future__ import annotations

import json
import logging
import os
import re
import uuid
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

_GROQ_BASE = "https://api.groq.com/openai/v1"
_DEFAULT_MODEL = "llama-3.3-70b-versatile"

# Llama sometimes outputs tool calls in its native format instead of OpenAI format.
# Groq rejects these and returns the raw generation under "failed_generation".
# We recover by finding each <function=name ...> block and extracting the JSON args
# from the first { ... } found after the function name — handles all separator variants:
#   <function=name>{"args"}          standard
#   <function=name={"args"}          = separator
#   <function=name,{"args"}          , separator
#   <function=name[]{"args"}         [] decorator
#   <function=name {"args"}          space separator
_FUNC_NAME_RE = re.compile(r"<function=(\w+)")


def _parse_native_tool_calls(failed_generation: str) -> list[ToolCall]:
    """Extract tool calls from Llama native format in a failed_generation string."""
    tool_calls = []
    for m in _FUNC_NAME_RE.finditer(failed_generation):
        name = m.group(1)
        rest = failed_generation[m.end():]

        brace = rest.find("{")
        if brace == -1:
            continue

        json_candidate = rest[brace:]
        end = json_candidate.find("</function>")
        if end != -1:
            json_candidate = json_candidate[:end]

        try:
            arguments = json.loads(json_candidate.strip())
            if not isinstance(arguments, dict):
                continue
        except json.JSONDecodeError:
            logger.debug("_parse_native_tool_calls: could not parse args for %r", name)
            continue

        tool_calls.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:8]}",  # unique id, never reuse tool name
                name=name,
                arguments=arguments,
            )
        )

    return tool_calls


class GroqClient(LLMClient):
    """Groq inference backend. OpenAI-compatible API, fast parallel calls."""

    def __init__(self, model: str | None = None, api_key: str | None = None) -> None:
        self.model = model or os.environ.get("GROQ_MODEL", _DEFAULT_MODEL)
        self._api_key = api_key or os.environ.get("GROQ_API_KEY", "")

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

        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{_GROQ_BASE}/chat/completions",
                headers=headers,
                json=payload,
            )

            if resp.status_code == 400:
                try:
                    error_data = resp.json()
                    failed = error_data.get("error", {}).get("failed_generation", "")
                    if failed:
                        tool_calls = _parse_native_tool_calls(failed)
                        if tool_calls:
                            logger.debug(
                                "GroqClient: recovered %d tool call(s) from failed_generation",
                                len(tool_calls),
                            )
                            return LLMResponse(content=None, tool_calls=tool_calls)
                except Exception:
                    pass
                logger.error("GroqClient: 400 — %s", resp.text)

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
                logger.debug("GroqClient: skipping malformed tool call: %s", tc)

        return LLMResponse(content=choice.get("content"), tool_calls=tool_calls)
