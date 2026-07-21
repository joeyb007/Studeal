"""Tests for nav/extract LLM builder split."""
import pytest

from dealbot.agents.composition import build_extract_llm, build_nav_llm
from dealbot.llm.groq_client import GroqClient
from dealbot.llm.openai_client import OpenAIClient


def test_nav_llm_defaults_to_gpt4o(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("AGENT_NAV_MODEL", raising=False)
    llm = build_nav_llm()
    assert isinstance(llm, OpenAIClient)
    assert llm.model == "gpt-4o"


def test_nav_llm_respects_env_override(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("AGENT_NAV_MODEL", "gpt-4.1")
    assert build_nav_llm().model == "gpt-4.1"


def test_extract_llm_defaults_to_mini(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    llm = build_extract_llm()
    assert isinstance(llm, OpenAIClient)
    assert llm.model == "gpt-4o-mini"


def test_builders_fall_back_to_groq(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert isinstance(build_nav_llm(), GroqClient)
    assert isinstance(build_extract_llm(), GroqClient)
