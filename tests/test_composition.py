"""Tests for nav/extract LLM builder split."""
import asyncio
import pytest

from dealbot.agents.composition import build_extract_llm, build_nav_llm, ThrottledLLM
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.openai_client import OpenAIClient


def test_nav_llm_defaults_to_gpt4o(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AGENT_NAV_MODEL", raising=False)
    llm = build_nav_llm()
    assert isinstance(llm, ThrottledLLM)
    assert isinstance(llm.inner, OpenAIClient)
    assert llm.inner.model == "gpt-4o"


def test_nav_llm_respects_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_NAV_MODEL", "gpt-4.1")
    llm = build_nav_llm()
    assert llm.inner.model == "gpt-4.1"


def test_extract_llm_defaults_to_mini(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    llm = build_extract_llm()
    assert isinstance(llm, OpenAIClient)
    assert llm.model == "gpt-4o-mini"


def test_builders_fall_back_to_groq(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    nav_llm = build_nav_llm()
    extract_llm = build_extract_llm()
    assert isinstance(nav_llm, ThrottledLLM)
    assert isinstance(nav_llm.inner, GroqClient)
    assert isinstance(extract_llm, GroqClient)


class _SlowLLM:
    supports_vision = True

    def __init__(self):
        self.active = 0
        self.peak = 0

    async def complete(self, messages, tools=None, response_format=None):
        self.active += 1
        self.peak = max(self.peak, self.active)
        await asyncio.sleep(0.02)
        self.active -= 1
        return None


@pytest.mark.asyncio
async def test_throttled_llm_caps_concurrency():
    inner = _SlowLLM()
    llm = ThrottledLLM(inner, max_concurrency=2)
    await asyncio.gather(*(llm.complete([]) for _ in range(8)))
    assert inner.peak <= 2


def test_throttled_llm_forwards_supports_vision():
    assert ThrottledLLM(_SlowLLM(), max_concurrency=1).supports_vision is True


def test_nav_llm_is_throttled(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_NAV_CONCURRENCY", "3")
    llm = build_nav_llm()
    assert isinstance(llm, ThrottledLLM)
