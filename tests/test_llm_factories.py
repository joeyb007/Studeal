"""Backend selection: LLM_BACKEND=bedrock must win explicitly, even when an
OPENAI_API_KEY is present — the composition factories historically keyed on
key-presence, which would silently ignore the configured backend."""

from __future__ import annotations


def test_watchlists_llm_factory_selects_bedrock(monkeypatch):
    from dealbot.api.routes.watchlists import _get_llm
    from dealbot.llm.bedrock_client import BedrockClient

    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-ignored")
    assert isinstance(_get_llm(), BedrockClient)


def test_nav_llm_prefers_bedrock_over_openai_key(monkeypatch):
    from dealbot.agents.composition import build_nav_llm
    from dealbot.llm.bedrock_client import BedrockClient

    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-ignored")
    monkeypatch.setenv("BEDROCK_NAV_MODEL", "us.anthropic.claude-sonnet-test")

    throttled = build_nav_llm()
    inner = throttled._inner if hasattr(throttled, "_inner") else throttled
    assert isinstance(inner, BedrockClient)
    assert inner.model == "us.anthropic.claude-sonnet-test"


def test_extract_llm_prefers_bedrock_over_openai_key(monkeypatch):
    from dealbot.agents.composition import build_extract_llm
    from dealbot.llm.bedrock_client import BedrockClient

    monkeypatch.setenv("LLM_BACKEND", "bedrock")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-be-ignored")
    monkeypatch.setenv("BEDROCK_EXTRACT_MODEL", "us.anthropic.claude-haiku-test")

    client = build_extract_llm()
    assert isinstance(client, BedrockClient)
    assert client.model == "us.anthropic.claude-haiku-test"


def test_openai_behavior_unchanged_without_backend_flag(monkeypatch):
    """Regression: with no LLM_BACKEND (or =openai), key-presence still rules."""
    from dealbot.agents.composition import build_extract_llm
    from dealbot.llm.openai_client import OpenAIClient

    monkeypatch.delenv("LLM_BACKEND", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert isinstance(build_extract_llm(), OpenAIClient)


def test_embedding_factory_selects_bedrock(monkeypatch):
    from dealbot.llm.bedrock_client import BedrockEmbeddingClient
    from dealbot.llm.embeddings import _get_client

    monkeypatch.setenv("EMBEDDING_BACKEND", "bedrock")
    assert isinstance(_get_client(), BedrockEmbeddingClient)
