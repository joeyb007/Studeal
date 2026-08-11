"""Bedrock backends — Claude chat via the Converse API, Titan V2 embeddings.

Contract: call sites written for the OpenAI client work unchanged.
    - `response_format={"type": "json_object"}` has no Converse equivalent;
      it is absorbed here — a strict-JSON system suffix plus an assistant
      prefill of "{" (Anthropic-supported forced start), with the brace
      re-prepended to the returned text so json.loads() sees a full object.
    - Token counters (`total_prompt_tokens` / `total_completion_tokens` /
      `call_count`) mirror OpenAIClient — the eval harness reads them for
      cost accounting.
    - Retry/backoff parity: ThrottlingException is Bedrock's 429.

Model IDs are env-configured (`BEDROCK_NAV_MODEL`, `BEDROCK_EXTRACT_MODEL`,
`BEDROCK_EMBED_MODEL`) — Bedrock uses regional inference-profile IDs that
vary by account/region; the defaults below are the us-east-1 profiles and
should be verified against the console (scripts/smoke_bedrock.py).
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import random
from typing import Any

from .base import LLMClient, LLMResponse, ToolCall
from .embeddings import EmbeddingClient

logger = logging.getLogger(__name__)

_DEFAULT_CHAT_MODEL = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
# Nav default — verify against the console; overridden via BEDROCK_NAV_MODEL.
DEFAULT_NAV_MODEL = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
# Fast nav tier: routine explore turns (scroll / next page). Lanes escalate
# to DEFAULT_NAV_MODEL on trouble. Overridden via BEDROCK_NAV_FAST_MODEL.
DEFAULT_NAV_FAST_MODEL = _DEFAULT_CHAT_MODEL
_DEFAULT_EMBED_MODEL = "amazon.titan-embed-text-v2:0"
_MAX_RETRIES = 4
_BACKOFF_SCHEDULE_S = (8.0, 24.0, 60.0, 120.0)
_RETRYABLE_CODES = {"ThrottlingException", "ModelNotReadyException", "ServiceUnavailableException"}

_JSON_INSTRUCTION = (
    "Respond ONLY with a single valid JSON object. No prose, no markdown "
    "fences, no text before or after the JSON."
)


def _to_converse(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """OpenAI-style messages → (Converse system blocks, Converse messages).

    Handles: system-role extraction, plain-string content, multimodal lists
    with data-URL images, assistant tool_calls → toolUse, and role=tool →
    toolResult (Converse carries tool results in a user turn).
    """
    system: list[dict[str, Any]] = []
    out: list[dict[str, Any]] = []

    for message in messages:
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            if isinstance(content, str) and content:
                system.append({"text": content})
            continue

        if role == "tool":
            block = {
                "toolResult": {
                    "toolUseId": message.get("tool_call_id", ""),
                    "content": [{"text": str(content or "")}],
                }
            }
            # Converse wants every toolResult for an assistant turn in ONE
            # user turn; consecutive tool messages merge into the last one.
            if out and out[-1]["role"] == "user" and "toolResult" in out[-1]["content"][-1]:
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
            continue

        blocks: list[dict[str, Any]] = []
        if isinstance(content, str) and content:
            blocks.append({"text": content})
        elif isinstance(content, list):
            for part in content:
                if part.get("type") == "text":
                    blocks.append({"text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    url = part.get("image_url", {}).get("url", "")
                    if url.startswith("data:image/"):
                        header, _, b64 = url.partition(",")
                        image_format = header.split("/")[1].split(";")[0]
                        blocks.append({
                            "image": {
                                "format": image_format,
                                "source": {"bytes": base64.b64decode(b64)},
                            }
                        })

        for tc in message.get("tool_calls") or []:
            try:
                arguments = tc["function"]["arguments"]
                if isinstance(arguments, str):
                    arguments = json.loads(arguments)
                blocks.append({
                    "toolUse": {
                        "toolUseId": tc["id"],
                        "name": tc["function"]["name"],
                        "input": arguments,
                    }
                })
            except (KeyError, json.JSONDecodeError):
                logger.debug("bedrock: skipping malformed tool_call %s", tc)

        if blocks:
            out.append({"role": role, "content": blocks})

    return system, out


def _to_tool_config(tools: list[dict[str, Any]]) -> dict[str, Any]:
    """OpenAI function-tool schema → Converse toolConfig."""
    specs = []
    for tool in tools:
        function = tool.get("function", tool)
        specs.append({
            "toolSpec": {
                "name": function["name"],
                "description": function.get("description", ""),
                "inputSchema": {"json": function.get("parameters", {"type": "object"})},
            }
        })
    return {"tools": specs}


class BedrockClient(LLMClient):
    """Claude on Bedrock via Converse. See module docstring for the contract."""

    supports_vision = True

    def __init__(self, model: str | None = None, region: str | None = None) -> None:
        self.model = model or os.environ.get("BEDROCK_EXTRACT_MODEL", _DEFAULT_CHAT_MODEL)
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")
        self.total_prompt_tokens: int = 0
        self.total_completion_tokens: int = 0
        self.call_count: int = 0

    def _client_factory(self):
        """Async context manager for a bedrock-runtime client. Overridable in
        tests; built lazily so importing this module never requires AWS creds."""
        import aioboto3

        return aioboto3.Session().client("bedrock-runtime", region_name=self._region)

    def _record_usage(self, data: dict) -> None:
        try:
            usage = data.get("usage") or {}
            prompt, completion = usage.get("inputTokens"), usage.get("outputTokens")
            if isinstance(prompt, int) and isinstance(completion, int):
                self.total_prompt_tokens += prompt
                self.total_completion_tokens += completion
                self.call_count += 1
        except Exception:
            pass

    async def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> LLMResponse:
        from botocore.exceptions import ClientError

        system, converse_messages = _to_converse(messages)

        json_mode = bool(response_format) and response_format.get("type") == "json_object"
        if json_mode:
            system = system + [{"text": _JSON_INSTRUCTION}]
            # Forced JSON start: prefill the assistant turn with "{".
            converse_messages = converse_messages + [
                {"role": "assistant", "content": [{"text": "{"}]}
            ]
        if not system:
            # Converse rejects an empty system list only when omitted key is
            # required — keep behavior uniform by always sending something
            # meaningful in JSON mode and omitting otherwise.
            system = []

        kwargs: dict[str, Any] = {
            "modelId": self.model,
            "messages": converse_messages,
            "inferenceConfig": {"maxTokens": 4096},
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["toolConfig"] = _to_tool_config(tools)

        data: dict[str, Any] | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                async with self._client_factory() as client:
                    data = await client.converse(**kwargs)
                break
            except ClientError as exc:
                code = exc.response.get("Error", {}).get("Code", "")
                if code in _RETRYABLE_CODES and attempt < _MAX_RETRIES:
                    wait = _BACKOFF_SCHEDULE_S[attempt] + random.uniform(0, 2)
                    logger.warning(
                        "BedrockClient: %s (attempt %d/%d); sleeping %.1fs",
                        code, attempt + 1, _MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    continue
                logger.error("BedrockClient: %s — %s", code, exc)
                raise

        assert data is not None  # loop either broke with data or raised
        self._record_usage(data)

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("output", {}).get("message", {}).get("content", []):
            if "text" in block:
                text_parts.append(block["text"])
            elif "toolUse" in block:
                tu = block["toolUse"]
                arguments = tu.get("input")
                if isinstance(arguments, dict):
                    tool_calls.append(ToolCall(
                        id=tu.get("toolUseId", ""), name=tu.get("name", ""),
                        arguments=arguments,
                    ))

        content = "".join(text_parts) if text_parts else None
        if json_mode and content is not None:
            # Re-prepend the prefilled brace so json.loads sees a full object.
            content = "{" + content.lstrip()
        return LLMResponse(content=content, tool_calls=tool_calls)


_DEFAULT_MM_EMBED_MODEL = "amazon.titan-embed-image-v1"


def build_mm_embed_payload(text: str | None, image_b64: str | None, dimensions: int = 1024) -> dict:
    """Titan Multimodal G1 request body: text, image, or both fuse into ONE
    vector. Pure so the shape is testable without the network."""
    payload: dict = {"embeddingConfig": {"outputEmbeddingLength": dimensions}}
    if text and text.strip():
        payload["inputText"] = text.strip()[:2000]
    if image_b64:
        payload["inputImage"] = image_b64
    return payload


class BedrockMultimodalEmbeddingClient(EmbeddingClient):
    """Titan Multimodal Embeddings G1 — 1024 dims, text + image fused into a
    single vector. Text-only inputs embed into the same space, so queries and
    intent documents stay comparable with photo-carrying listings."""

    _DIMENSIONS = 1024
    _CONCURRENCY = 8

    def __init__(self, model: str | None = None, region: str | None = None) -> None:
        self.model = model or os.environ.get("BEDROCK_MM_EMBED_MODEL", _DEFAULT_MM_EMBED_MODEL)
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")

    def _client_factory(self):
        import aioboto3

        return aioboto3.Session().client("bedrock-runtime", region_name=self._region)

    async def _invoke(self, payload: dict) -> list[float]:
        try:
            async with self._client_factory() as client:
                response = await client.invoke_model(
                    modelId=self.model, body=json.dumps(payload),
                )
                data = json.loads(await response["body"].read())
                embedding = data.get("embedding")
                return embedding if isinstance(embedding, list) else []
        except Exception:
            logger.warning("BedrockMultimodalEmbeddingClient: invoke failed", exc_info=True)
            return []

    async def embed(self, text: str) -> list[float]:
        return await self._invoke(build_mm_embed_payload(text, None, self._DIMENSIONS))

    async def embed_with_image(self, text: str, image: bytes) -> list[float]:
        b64 = base64.b64encode(image).decode("ascii")
        return await self._invoke(build_mm_embed_payload(text, b64, self._DIMENSIONS))

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        semaphore = asyncio.Semaphore(self._CONCURRENCY)

        async def _one(t: str) -> list[float]:
            async with semaphore:
                return await self.embed(t)

        return list(await asyncio.gather(*(_one(t) for t in texts)))


class BedrockEmbeddingClient(EmbeddingClient):
    """Titan Text Embeddings V2 — 1024 dims.

    Phase-1 note: EMBED_DIM is still 1536; selecting this backend before
    migration 0025 fails loudly at the pgvector write (dimension mismatch),
    never silently. That is intended.
    """

    _DIMENSIONS = 1024
    _CONCURRENCY = 8  # Titan has no batch endpoint; bounded fan-out instead.

    def __init__(self, model: str | None = None, region: str | None = None) -> None:
        self.model = model or os.environ.get("BEDROCK_EMBED_MODEL", _DEFAULT_EMBED_MODEL)
        self._region = region or os.environ.get("AWS_REGION", "us-east-1")

    def _client_factory(self):
        import aioboto3

        return aioboto3.Session().client("bedrock-runtime", region_name=self._region)

    async def embed(self, text: str) -> list[float]:
        try:
            async with self._client_factory() as client:
                response = await client.invoke_model(
                    modelId=self.model,
                    body=json.dumps({
                        "inputText": text,
                        "dimensions": self._DIMENSIONS,
                        "normalize": True,
                    }),
                )
                payload = json.loads(await response["body"].read())
                embedding = payload.get("embedding")
                return embedding if isinstance(embedding, list) else []
        except Exception:
            logger.warning("BedrockEmbeddingClient: embed failed", exc_info=True)
            return []

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        semaphore = asyncio.Semaphore(self._CONCURRENCY)

        async def _one(t: str) -> list[float]:
            async with semaphore:
                return await self.embed(t)

        return list(await asyncio.gather(*(_one(t) for t in texts)))
