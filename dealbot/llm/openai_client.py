from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from typing import Any

import httpx

from .base import LLMClient, LLMResponse, ToolCall

logger = logging.getLogger(__name__)

_OPENAI_BASE = "https://api.openai.com/v1"
_DEFAULT_MODEL = "gpt-4o-mini"
_MAX_RETRIES_ON_RATE_LIMIT = 4
_BACKOFF_SCHEDULE_S = (8.0, 24.0, 60.0, 120.0)  # tries 1, 2, 3, 4
_HTTP_TIMEOUT_S = 60.0  # bumped from 30s for large prompts


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

        # Retry on 429 (rate limit) and transient network errors with backoff.
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES_ON_RATE_LIMIT + 1):
            try:
                async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_S) as client:
                    resp = await client.post(
                        f"{_OPENAI_BASE}/chat/completions",
                        headers=headers,
                        json=payload,
                    )
                if resp.status_code == 429 and attempt < _MAX_RETRIES_ON_RATE_LIMIT:
                    # Honor Retry-After header if present, else use backoff schedule.
                    retry_after = resp.headers.get("retry-after")
                    if retry_after and retry_after.replace(".", "", 1).isdigit():
                        wait = float(retry_after) + random.uniform(0, 1)
                    else:
                        wait = _BACKOFF_SCHEDULE_S[attempt] + random.uniform(0, 2)
                    logger.warning(
                        "OpenAIClient: 429 rate-limited (attempt %d/%d); sleeping %.1fs",
                        attempt + 1, _MAX_RETRIES_ON_RATE_LIMIT, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                if not resp.is_success:
                    logger.error("OpenAIClient: %s — %s", resp.status_code, resp.text)
                resp.raise_for_status()
                data = resp.json()
                break
            except (httpx.ReadTimeout, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
                if attempt < _MAX_RETRIES_ON_RATE_LIMIT:
                    wait = _BACKOFF_SCHEDULE_S[attempt] + random.uniform(0, 2)
                    logger.warning(
                        "OpenAIClient: %s (attempt %d/%d); sleeping %.1fs",
                        type(exc).__name__, attempt + 1, _MAX_RETRIES_ON_RATE_LIMIT, wait,
                    )
                    await asyncio.sleep(wait)
                    last_exc = exc
                    continue
                raise
        else:
            # Loop exhausted without a break.
            if last_exc is not None:
                raise last_exc

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
