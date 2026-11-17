"""Official SDK wire contract under a controlled HTTP transport; zero real models."""
import json

import httpx
import pytest
from langchain_deepseek import ChatDeepSeek
from langchain_openai import ChatOpenAI

from owcopilot.llm.chat_model import ChatModelProvider


@pytest.mark.parametrize("adapter", ["deepseek", "openai_compatible"])
def test_native_thinking_content_and_tool_ids_survive_sdk_roundtrip(adapter):
    sent = []
    def respond(request):
        sent.append(json.loads(request.content))
        first = len(sent) == 1
        message = {"role": "assistant", "content": "" if first else "Observed actual tool result",
                   "reasoning_content": (
                       "controlled protocol sample" if first else "controlled continuation"
                   )}
        if first:
            message["tool_calls"] = [{"id": "call-observe", "type": "function",
                "function": {"name": "inspect_branch", "arguments": '{"branch_id":"left"}'}}]
        return httpx.Response(200, json={"id": "controlled-response", "object": "chat.completion",
            "created": 1, "model": "controlled-deepseek", "choices": [
                {"index": 0, "message": message,
                 "finish_reason": "tool_calls" if first else "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}})
    client = httpx.Client(transport=httpx.MockTransport(respond))
    chat_type = ChatDeepSeek if adapter == "deepseek" else ChatOpenAI
    chat = chat_type(model="controlled-deepseek", api_key="controlled-fixture-key",
                        base_url="https://api.deepseek.com", http_client=client, max_retries=0)
    provider = ChatModelProvider("controlled-deepseek", provider_kind=adapter, chat_model=chat,
                                 max_retries=0)
    provider.base_url = "https://api.deepseek.com"
    # Exercise the production official-client branch, with HTTP fully controlled.
    provider._injected_chat = False
    tools = [{"type": "function", "function": {"name": "inspect_branch",
        "parameters": {"type": "object", "properties": {"branch_id": {"type": "string"}},
                       "required": ["branch_id"], "additionalProperties": False}}}]
    history = [{"role": "user", "content": "Inspect the left branch"}]
    first = provider.complete_with_tools(messages=history, tools=tools, model=provider.model)
    assert first.reasoning_content == "controlled protocol sample"
    history += [{"role": "assistant", "content": first.text,
                 "reasoning_content": first.reasoning_content,
                 "tool_calls": [{"id": first.tool_calls[0].call_id, "type": "function",
                     "function": {"name": "inspect_branch", "arguments": '{"branch_id":"left"}'}}]},
                {"role": "tool", "tool_call_id": "call-observe", "content": '{"ending":"left"}'}]
    second = provider.complete_with_tools(messages=history, tools=tools, model=provider.model)
    assert second.text == "Observed actual tool result"
    assert second.reasoning_content == "controlled continuation"
    assert sent[1]["messages"][1]["reasoning_content"] == first.reasoning_content
    assert sent[1]["messages"][1]["tool_calls"][0]["id"] == "call-observe"
    assert sent[1]["messages"][2]["tool_call_id"] == "call-observe"
    assert not any("thinking" in body and body["thinking"] == {"type": "disabled"} for body in sent)
    client.close()
