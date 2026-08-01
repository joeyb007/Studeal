"""Bedrock go/no-go — one live call per model, run when AWS keys land.

Verifies: credentials work, model access is granted, the configured model IDs
exist in this region, JSON mode round-trips, and Titan returns 1024 dims.
Cost: well under a cent.

Usage:
    venv/bin/python scripts/smoke_bedrock.py

Env (from .env): AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION,
optional BEDROCK_NAV_MODEL / BEDROCK_EXTRACT_MODEL / BEDROCK_EMBED_MODEL.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from dotenv import load_dotenv  # noqa: E402

load_dotenv(REPO_ROOT / ".env")

from dealbot.llm.bedrock_client import (  # noqa: E402
    DEFAULT_NAV_MODEL,
    BedrockClient,
    BedrockEmbeddingClient,
)


async def _smoke_chat(label: str, model: str) -> bool:
    client = BedrockClient(model=model)
    start = time.monotonic()
    try:
        response = await client.complete(
            [{"role": "user", "content": 'Reply with exactly {"ok": true}'}],
            response_format={"type": "json_object"},
        )
        elapsed = time.monotonic() - start
        parsed = json.loads(response.content or "{}")
        ok = parsed.get("ok") is True
        print(f"  [{'PASS' if ok else 'WARN'}] {label}: {model}")
        print(f"         {elapsed:.1f}s · {client.total_prompt_tokens} in / "
              f"{client.total_completion_tokens} out · content={response.content!r}")
        return ok
    except Exception as exc:
        print(f"  [FAIL] {label}: {model}\n         {type(exc).__name__}: {exc}")
        return False


async def _smoke_embed() -> bool:
    client = BedrockEmbeddingClient()
    start = time.monotonic()
    vector = await client.embed("used herman miller aeron chair toronto")
    elapsed = time.monotonic() - start
    if len(vector) == 1024:
        print(f"  [PASS] embed: {client.model}\n         {elapsed:.1f}s · {len(vector)} dims")
        return True
    print(f"  [FAIL] embed: {client.model} — got {len(vector)} dims (want 1024)")
    return False


async def main() -> int:
    if not (os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE")):
        print("No AWS credentials in env — add AWS_ACCESS_KEY_ID / "
              "AWS_SECRET_ACCESS_KEY (or AWS_PROFILE) to .env first.")
        return 1

    print(f"region: {os.environ.get('AWS_REGION', 'us-east-1')}\n")
    results = [
        await _smoke_chat(
            "nav    ", os.environ.get("BEDROCK_NAV_MODEL", DEFAULT_NAV_MODEL)),
        await _smoke_chat(
            "extract", BedrockClient().model),
        await _smoke_embed(),
    ]
    print("\nGO" if all(results) else "\nNO-GO — fix the failures above "
          "(wrong model ID and missing model-access grants are the usual causes)")
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
