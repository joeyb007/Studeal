from __future__ import annotations

import pytest

from dealbot.config import validate_env


def test_validate_env_passes_with_all_required(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "supersecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    validate_env()  # should not raise


def test_validate_env_raises_on_missing_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "supersecret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    with pytest.raises(EnvironmentError, match="DATABASE_URL"):
        validate_env()


def test_validate_env_raises_on_missing_secret_key(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delenv("SECRET_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    with pytest.raises(EnvironmentError, match="SECRET_KEY"):
        validate_env()


def test_validate_env_raises_on_missing_openai_key_when_backend_is_openai(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "supersecret")
    monkeypatch.setenv("LLM_BACKEND", "openai")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    with pytest.raises(EnvironmentError, match="OPENAI_API_KEY"):
        validate_env()


def test_validate_env_skips_openai_key_when_backend_is_groq(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv("SECRET_KEY", "supersecret")
    monkeypatch.setenv("LLM_BACKEND", "groq")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("BROWSERBASE_API_KEY", "bb-test")
    monkeypatch.setenv("BROWSERBASE_PROJECT_ID", "proj-test")
    validate_env()  # should not raise — groq doesn't need OPENAI_API_KEY
