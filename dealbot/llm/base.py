from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from pydantic import BaseModel


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class LLMResponse(BaseModel):
    content: Optional[str] = None
    tool_calls: list[ToolCall] = []


class LLMClient(ABC):
    """Abstract base for all LLM backends."""

    @abstractmethod
    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse: ...
