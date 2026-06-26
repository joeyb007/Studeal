"""Shared helpers for LLM JSON-mode calls used by simple workers.

Pattern: ask LLM for JSON, parse into a Pydantic model, retry on parse failure
up to `_MAX_RETRIES` times with progressively stronger corrective prompts that
include the specific error from the prior attempt. If all retries still fail,
raise — caller decides whether to drop the worker call or surface the failure
into the trajectory.

Retry budget was bumped 1 → 3 after spike data showed OfferExtractor's LLM
intermittently produces malformed JSON (~30% of calls). Three retries catch
most stochastic failures without root-causing a model-specific bug.
"""

from __future__ import annotations

import json
import logging
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from dealbot.llm.base import LLMClient

logger = logging.getLogger(__name__)

_T = TypeVar("_T", bound=BaseModel)

# Including the initial call, this is up-to-4 total attempts.
_MAX_RETRIES = 3


class WorkerOutputError(Exception):
    """Raised when a worker's LLM call can't produce parseable structured output."""


async def call_with_json_output(
    llm: LLMClient,
    system_prompt: str,
    user_prompt: str,
    schema: type[_T],
) -> _T:
    """Run an LLM call expecting JSON output, validate via Pydantic, retry on
    parse failure with the specific error fed back to the LLM each time.

    Returns the parsed Pydantic model on success. Raises WorkerOutputError if
    all attempts fail.
    """
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    last_response_content = ""
    last_error_msg = ""

    for attempt in range(_MAX_RETRIES + 1):
        # On retries, feed the prior bad output + specific error back as a
        # corrective user message so the LLM can self-correct.
        if attempt > 0:
            messages.append({"role": "assistant", "content": last_response_content})
            messages.append({"role": "user", "content": (
                f"Your previous response did not parse as valid {schema.__name__} JSON. "
                f"Specific error: {last_error_msg}. "
                "Re-emit JSON only, conforming exactly to the schema. "
                "No prose, no markdown fences, no commentary."
            )})

        response = await llm.complete(
            messages, response_format={"type": "json_object"},
        )
        last_response_content = response.content or ""
        parsed, error = _try_parse_with_error(last_response_content, schema)
        if parsed is not None:
            if attempt > 0:
                logger.info(
                    "worker: parse succeeded on retry attempt %d for %s",
                    attempt, schema.__name__,
                )
            return parsed
        last_error_msg = error or "unknown parse failure"

    raise WorkerOutputError(
        f"LLM produced non-conforming output after {_MAX_RETRIES} retries. "
        f"Schema={schema.__name__}. Last error: {last_error_msg}. "
        f"Response length={len(last_response_content)} chars. "
        f"Full content:\n---\n{last_response_content}\n---"
    )


def _try_parse_with_error(text: str, schema: type[_T]) -> tuple[_T | None, str | None]:
    """Attempt to parse `text` as JSON matching `schema`.

    Returns (parsed, None) on success or (None, error_message) on failure so
    the caller can feed the specific error back to the LLM in the corrective
    retry prompt. Also logs at WARNING so spike traces surface the failures.
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"JSON decode failed: {exc}"
        logger.warning(
            "worker: %s for %s (text_len=%d, head=%r)",
            msg, schema.__name__, len(text or ""), (text or "")[:300],
        )
        return None, msg
    try:
        return schema.model_validate(data), None
    except ValidationError as exc:
        # First 2 errors are usually enough for the LLM to self-correct.
        err_summary = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
            for e in exc.errors()[:2]
        )
        msg = f"schema validation failed: {err_summary}"
        logger.warning(
            "worker: %s for %s",
            msg, schema.__name__,
        )
        return None, msg
