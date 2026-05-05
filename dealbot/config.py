from __future__ import annotations

import os


_ALWAYS_REQUIRED = [
    "DATABASE_URL",
    "REDIS_URL",
    "SECRET_KEY",
    "BROWSERBASE_API_KEY",
    "BROWSERBASE_PROJECT_ID",
]

_BACKEND_KEYS: dict[str, list[str]] = {
    "openai": ["OPENAI_API_KEY"],
    "groq": ["GROQ_API_KEY"],
    "vllm": ["VLLM_BASE_URL"],
    "ollama": [],
}


def validate_env() -> None:
    """Raise EnvironmentError on first missing required variable."""
    missing = [k for k in _ALWAYS_REQUIRED if not os.environ.get(k)]
    if missing:
        raise EnvironmentError(
            f"Missing required environment variable(s): {', '.join(missing)}"
        )

    backend = os.environ.get("LLM_BACKEND", "openai")
    backend_required = _BACKEND_KEYS.get(backend, [])
    missing_backend = [k for k in backend_required if not os.environ.get(k)]
    if missing_backend:
        raise EnvironmentError(
            f"LLM_BACKEND={backend!r} requires: {', '.join(missing_backend)}"
        )
