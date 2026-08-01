"""BedrockClient — Claude via the Converse API behind the LLMClient ABC.

The contract that matters: call sites written for OpenAI (json_object
response_format, OpenAI message shapes, token counters) work unchanged.
All AWS traffic is mocked — no test dials Bedrock.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from dealbot.llm.bedrock_client import BedrockClient, _to_converse


class _FakeBedrockRuntime:
    """Records converse() kwargs; returns a canned Converse response."""

    def __init__(self, response: dict | None = None, raises: list[Exception] | None = None):
        self.response = response or _text_response("hello")
        self.raises = list(raises or [])
        self.calls: list[dict[str, Any]] = []

    async def converse(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises.pop(0)
        return self.response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _text_response(text: str, in_tokens: int = 10, out_tokens: int = 5) -> dict:
    return {
        "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
        "usage": {"inputTokens": in_tokens, "outputTokens": out_tokens},
        "stopReason": "end_turn",
    }


def _client(fake: _FakeBedrockRuntime) -> BedrockClient:
    client = BedrockClient(model="us.anthropic.claude-test")
    client._client_factory = lambda: fake  # bypass aioboto3 session
    return client


# ---------------------------------------------------------------------------
# Message translation (pure function)
# ---------------------------------------------------------------------------

def test_system_messages_split_out():
    system, messages = _to_converse([
        {"role": "system", "content": "be terse"},
        {"role": "user", "content": "hi"},
    ])
    assert system == [{"text": "be terse"}]
    assert messages == [{"role": "user", "content": [{"text": "hi"}]}]


def test_data_url_images_become_image_blocks():
    png = base64.b64encode(b"fakepng").decode()
    _system, messages = _to_converse([
        {"role": "user", "content": [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{png}"}},
        ]},
    ])
    blocks = messages[0]["content"]
    assert blocks[0] == {"text": "what is this"}
    assert blocks[1]["image"]["format"] == "png"
    assert blocks[1]["image"]["source"]["bytes"] == b"fakepng"


def test_assistant_tool_calls_and_tool_results_round_trip():
    _system, messages = _to_converse([
        {"role": "assistant", "content": None, "tool_calls": [
            {"id": "t1", "function": {"name": "click", "arguments": '{"id": 42}'}},
        ]},
        {"role": "tool", "tool_call_id": "t1", "content": "clicked"},
    ])
    assert messages[0]["content"] == [
        {"toolUse": {"toolUseId": "t1", "name": "click", "input": {"id": 42}}}
    ]
    assert messages[1]["role"] == "user"
    assert messages[1]["content"][0]["toolResult"]["toolUseId"] == "t1"


# ---------------------------------------------------------------------------
# complete()
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_complete_returns_text_and_counts_tokens():
    fake = _FakeBedrockRuntime(_text_response("the answer", 100, 25))
    client = _client(fake)

    response = await client.complete([{"role": "user", "content": "q"}])

    assert response.content == "the answer"
    assert client.total_prompt_tokens == 100
    assert client.total_completion_tokens == 25
    assert client.call_count == 1


@pytest.mark.asyncio
async def test_json_mode_prefills_and_reconstructs_the_brace():
    """response_format=json_object has no Converse equivalent. The client
    prefills the assistant turn with "{" and must re-prepend it on return,
    so call sites json.loads() the content unchanged."""
    fake = _FakeBedrockRuntime(_text_response('"queries": ["a"]}'))
    client = _client(fake)

    response = await client.complete(
        [{"role": "user", "content": "emit json"}],
        response_format={"type": "json_object"},
    )

    sent = fake.calls[0]
    assert sent["messages"][-1] == {"role": "assistant", "content": [{"text": "{"}]}
    assert "JSON" in sent["system"][0]["text"]
    assert json.loads(response.content) == {"queries": ["a"]}


@pytest.mark.asyncio
async def test_json_mode_without_system_message_still_gets_instruction():
    fake = _FakeBedrockRuntime(_text_response("}"))
    client = _client(fake)
    await client.complete(
        [{"role": "user", "content": "q"}], response_format={"type": "json_object"},
    )
    assert "JSON" in fake.calls[0]["system"][0]["text"]


@pytest.mark.asyncio
async def test_tools_translate_to_tool_config_and_back():
    fake = _FakeBedrockRuntime({
        "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": "u1", "name": "click", "input": {"id": 7}}},
        ]}},
        "usage": {"inputTokens": 1, "outputTokens": 1},
        "stopReason": "tool_use",
    })
    client = _client(fake)

    response = await client.complete(
        [{"role": "user", "content": "click search"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "click",
                "description": "click an element",
                "parameters": {"type": "object", "properties": {"id": {"type": "integer"}}},
            },
        }],
    )

    spec = fake.calls[0]["toolConfig"]["tools"][0]["toolSpec"]
    assert spec["name"] == "click"
    assert spec["inputSchema"]["json"]["properties"]["id"]["type"] == "integer"
    assert response.tool_calls[0].name == "click"
    assert response.tool_calls[0].arguments == {"id": 7}
    assert response.tool_calls[0].id == "u1"


@pytest.mark.asyncio
async def test_retries_on_throttling_then_succeeds(monkeypatch):
    from botocore.exceptions import ClientError

    throttle = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "Converse",
    )
    fake = _FakeBedrockRuntime(_text_response("ok"), raises=[throttle])
    client = _client(fake)

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr("dealbot.llm.bedrock_client.asyncio.sleep", _no_sleep)
    response = await client.complete([{"role": "user", "content": "q"}])

    assert response.content == "ok"
    assert len(fake.calls) == 2


@pytest.mark.asyncio
async def test_non_throttle_client_errors_raise(monkeypatch):
    from botocore.exceptions import ClientError

    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no model access"}},
        "Converse",
    )
    fake = _FakeBedrockRuntime(raises=[denied])
    client = _client(fake)

    with pytest.raises(ClientError):
        await client.complete([{"role": "user", "content": "q"}])
    assert len(fake.calls) == 1, "access errors must not burn retries"
