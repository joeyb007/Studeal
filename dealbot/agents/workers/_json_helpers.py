"""Shared helpers for LLM JSON-mode calls used by simple workers.

Pattern: ask LLM for JSON, parse into a Pydantic model, retry once with the
parser error fed back as a corrective message. If parsing still fails on
retry, raise — caller decides whether to drop the worker call or surface the
failure into the trajectory.
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=BaseModel)


class WorkerOutputError(Exception):
    """Raised when a worker's LLM call can't produce parseable structured output."""


async def call_with_json_output(
    llm: LLMClient,
    system_prompt: str,
    user_prompt: str,
    schema: type[_T],
) -> _T:
    """Run an LLM call expecting JSON output, validate via Pydantic, retry once.

    Returns the parsed Pydantic model on success. Raises WorkerOutputError if
    both the initial call and the retry fail to produce valid output.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    response = await llm.complete(
        messages, response_format={"type": "json_object"},
    )
    parsed = _try_parse(response.content, schema)
    if parsed is not None:
        return parsed

    # Retry once with the error fed back.
    messages.append({"role": "assistant", "content": response.content})
    messages.append({"role": "user", "content": (
        "Your previous response wasn't valid JSON conforming to the schema. "
        "Reread the schema and try again. Output JSON only."
    )})
    response = await llm.complete(
        messages, response_format={"type": "json_object"},
    )
    parsed = _try_parse(response.content, schema)
    if parsed is not None:
        return parsed

    raise WorkerOutputError(
        f"LLM produced non-conforming output after retry. Schema={schema.__name__}. "
        f"Got: {response.content[:200]!r}"
    )


def _try_parse(text: str, schema: type[_T]) -> _T | None:
    """Attempt to parse `text` as JSON matching `schema`. Returns None on failure."""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        logger.debug("worker: JSON decode failed: %s", exc)
        return None
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        logger.debug("worker: schema validation failed: %s", exc)
        return None
