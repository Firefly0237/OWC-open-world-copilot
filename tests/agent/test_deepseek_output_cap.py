"""Official SDK wire caps under MockTransport; no vendor requests or human evidence."""

from __future__ import annotations

import json

import httpx
import openai
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from owcopilot.llm.chat_model import ChatModelProvider, build_chat_model
from owcopilot.llm.gateway import OutputTruncatedError


@pytest.fixture
def sdk_wire(monkeypatch):
    sent = []
    real_client = openai.OpenAI
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")

    def respond(request):
        body = json.loads(request.content)
        sent.append(body)
        return httpx.Response(
            200,
            json={
                "id": "controlled-cap",
                "object": "chat.completion",
                "created": 1,
                "model": "controlled-deepseek",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": '{"ok":true}',
                            "reasoning_content": "controlled reasoning field",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
            },
        )

    transport = httpx.Client(transport=httpx.MockTransport(respond))

    def controlled_client(**kwargs):
        kwargs["http_client"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(openai, "OpenAI", controlled_client)
    yield sent
    transport.close()


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.deepseek.com",
        "https://api.deepseek.com/v1",
        "https://api.deepseek.com/beta",
    ],
)
def test_official_deepseek_cap_reaches_selected_openai_sdk_wire(base_url, sdk_wire):
    chat = build_chat_model(
        model="controlled-deepseek",
        api_key="controlled-fixture-key",
        base_url=base_url,
        provider_kind="openai_compatible",
        max_tokens=3500,
        max_retries=0,
        timeout=7.5,
    )
    assert isinstance(chat, ChatOpenAI)
    assert chat.max_retries == chat.root_client.max_retries == 0
    assert chat.request_timeout == 7.5
    chat.invoke(
        [HumanMessage(content="Return a JSON object")],
        response_format={"type": "json_object"},
        extra_body={"thinking": {"type": "enabled"}},
    )
    assert sdk_wire[0]["max_tokens"] == 3500
    assert "max_completion_tokens" not in sdk_wire[0]
    assert sdk_wire[0]["model"] == "controlled-deepseek"
    assert sdk_wire[0]["response_format"] == {"type": "json_object"}
    assert sdk_wire[0]["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize(
    "base_url",
    [
        "https://api.openai.com/v1",
        "https://api.deepseek.com.example/v1",
        "http://localhost:9/v1",
    ],
)
def test_other_compatible_hosts_keep_their_original_output_field(base_url, sdk_wire):
    chat = build_chat_model(
        model="controlled-deepseek",
        api_key="controlled-fixture-key",
        base_url=base_url,
        provider_kind="openai_compatible",
        max_tokens=3500,
        max_retries=0,
    )
    chat.invoke([HumanMessage(content="A controlled request")])
    assert sdk_wire[0]["max_completion_tokens"] == 3500
    assert "max_tokens" not in sdk_wire[0]


def test_official_endpoint_honors_bound_cap_and_omits_an_unset_cap(sdk_wire):
    chat = build_chat_model(
        model="controlled-deepseek",
        api_key="controlled-fixture-key",
        base_url="https://api.deepseek.com",
        provider_kind="openai_compatible",
        max_tokens=3500,
        max_retries=0,
    )
    chat.bind(max_tokens=32).invoke([HumanMessage(content="A bounded request")])
    assert sdk_wire[0]["max_tokens"] == 32
    assert "max_completion_tokens" not in sdk_wire[0]
    uncapped = build_chat_model(
        model="controlled-deepseek",
        api_key="controlled-fixture-key",
        base_url="https://api.deepseek.com",
        provider_kind="openai_compatible",
        max_retries=0,
    )
    uncapped.invoke([HumanMessage(content="Preserve an unspecified limit")])
    assert "max_tokens" not in sdk_wire[1] and "max_completion_tokens" not in sdk_wire[1]


def test_native_deepseek_adapter_retains_its_sdk_cap(sdk_wire):
    native_type = pytest.importorskip("langchain_deepseek").ChatDeepSeek
    chat = build_chat_model(
        model="controlled-deepseek",
        api_key="controlled-fixture-key",
        provider_kind="deepseek",
        max_tokens=3500,
        max_retries=0,
    )
    assert isinstance(chat, native_type)
    chat.invoke([HumanMessage(content="A native SDK request")])
    assert sdk_wire[0]["max_tokens"] == 3500
    assert "max_completion_tokens" not in sdk_wire[0]


def test_provider_text_and_thinking_tool_history_share_the_correct_cap(sdk_wire, monkeypatch):
    monkeypatch.setenv("OWCOPILOT_CAP_TEST_URL", "https://api.deepseek.com")
    monkeypatch.setenv("OWCOPILOT_CAP_TEST_KEY", "controlled-fixture-key")
    provider = ChatModelProvider(
        "controlled-deepseek",
        provider_kind="openai_compatible",
        base_url_env="CAP_TEST_URL",
        api_key_env="CAP_TEST_KEY",
        max_retries=0,
        timeout=7.5,
        max_output_tokens=3500,
    )
    text = provider.complete(system="Return JSON", user="A controlled request", model="cheap")
    assert text[0] == '{"ok":true}'
    history = [
        {"role": "user", "content": "Inspect the chosen branch"},
        {
            "role": "assistant",
            "content": "",
            "reasoning_content": "controlled prior reasoning",
            "tool_calls": [
                {
                    "id": "call-inspect",
                    "type": "function",
                    "function": {"name": "inspect_branch", "arguments": '{"id":"left"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call-inspect", "content": '{"ending":"left"}'},
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "inspect_branch",
                "parameters": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        }
    ]
    result = provider.complete_with_tools(messages=history, tools=tools, model="cheap")
    assert result.reasoning_content == "controlled reasoning field"
    assert provider.provider_kind == "openai_compatible"
    assert len(sdk_wire) == 2
    assert all(
        body["max_tokens"] == 3500 and "max_completion_tokens" not in body for body in sdk_wire
    )
    assert sdk_wire[1]["messages"][1]["reasoning_content"] == "controlled prior reasoning"
    assert sdk_wire[1]["messages"][1]["tool_calls"][0]["id"] == "call-inspect"
    assert sdk_wire[1]["messages"][2]["tool_call_id"] == "call-inspect"
    assert not any(body.get("thinking") == {"type": "disabled"} for body in sdk_wire)


def test_length_finish_still_raises_the_existing_truncation_error(monkeypatch):
    sent = []
    real_client = openai.OpenAI

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "controlled-length",
                "object": "chat.completion",
                "created": 1,
                "model": "controlled-deepseek",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "length",
                        "message": {"role": "assistant", "content": "unfinished"},
                    }
                ],
                "usage": {"prompt_tokens": 2, "completion_tokens": 32, "total_tokens": 34},
            },
        )

    with httpx.Client(transport=httpx.MockTransport(respond)) as transport:

        def controlled_client(**kwargs):
            return real_client(**{**kwargs, "http_client": transport})

        monkeypatch.setattr(openai, "OpenAI", controlled_client)
        monkeypatch.setenv("OWCOPILOT_CAP_TEST_URL", "https://api.deepseek.com")
        monkeypatch.setenv("OWCOPILOT_CAP_TEST_KEY", "controlled-fixture-key")
        provider = ChatModelProvider(
            "controlled-deepseek",
            provider_kind="openai_compatible",
            base_url_env="CAP_TEST_URL",
            api_key_env="CAP_TEST_KEY",
            max_retries=0,
            max_output_tokens=32,
            json_mode=False,
        )
        with pytest.raises(OutputTruncatedError):
            provider.complete(system="Keep it short", user="A controlled request", model="cheap")
    assert len(sent) == 1 and sent[0]["max_tokens"] == 32
