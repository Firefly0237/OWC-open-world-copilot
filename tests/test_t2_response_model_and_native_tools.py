"""Team 2 (llm/ + agent/react.py): gen_ai.response.model + native tool-calling.

Three concerns, all offline / $0:
  P1  response.model — the API response-body model id rides the provider tuple → CallRecord
      → OTEL gen_ai.response.model; failover surfaces the SECONDARY's model; offline fakes leave
      it unset.
  P2a native tool-calling — opt-in structured loop; default False is byte-identical to the text
      path; an unsupported provider transparently falls back to text.
  P3a tokenizer — the approximation is honestly documented.
"""

from __future__ import annotations

from typing import Any

import pytest

from owcopilot.core.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillParameter,
    SkillRegistry,
)
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import (
    LLMGateway,
    LLMGatewayError,
    ToolCall,
    ToolCallResponse,
    _parse_tool_response,
    provider_supports_tools,
)
from owcopilot.llm.resilience import FailoverProvider
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector

# --------------------------------------------------------------------------- helpers


def _gateway(provider: Any, *, task: str = "generate") -> tuple[LLMGateway, TelemetryCollector]:
    telemetry = TelemetryCollector()
    gw = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=NoOpCache(),
        telemetry=telemetry,
    )
    return gw, telemetry


class _ProviderWithResponseModel:
    """A provider exposing a configured (request-side) ``.model`` and returning a DIFFERENT
    response-body model id as the tuple's 5th element — exactly the divergence response.model
    must capture (e.g. an API silently routing an alias to a concrete model)."""

    model = "deepseek-v4-flash"  # what we requested (request.model)

    def __init__(self, response_model: str) -> None:
        self.response_model = response_model

    def complete(self, *, system: str, user: str, model: str):
        return "ok", 5, 3, 0, self.response_model


class _LegacyThreeTupleProvider:
    """An offline-fake-shaped provider that returns the old 3-tuple (no response model)."""

    def complete(self, *, system: str, user: str, model: str):
        return "ok", 1, 1


# --------------------------------------------------------------------------- P1: response.model


def test_response_model_recorded_from_provider_tuple() -> None:
    gw, telemetry = _gateway(_ProviderWithResponseModel("deepseek-v4-flash-0613"))
    gw.complete(task="generate", system="s", user="u")
    rec = telemetry.records[-1]
    # request side stays the configured model; response side is what the body reported.
    assert rec.model == "deepseek-v4-flash"
    assert rec.response_model == "deepseek-v4-flash-0613"


def test_response_model_empty_for_offline_three_tuple() -> None:
    # An offline fake can't report a response model → response_model must stay "" (never guessed).
    gw, telemetry = _gateway(_LegacyThreeTupleProvider())
    gw.complete(task="generate", system="s", user="u")
    assert telemetry.records[-1].response_model == ""


def test_response_model_follows_failover_to_secondary() -> None:
    # The canon insight: on failover the response body comes from the SECONDARY, so its model id
    # rides out naturally — no wrapper has to guess who answered.
    primary = _FailingProvider("deepseek-v4-pro", RuntimeError("connection refused"))
    secondary = _ProviderWithResponseModel("gpt-4o-mini")
    fp = FailoverProvider(primary, secondary)
    gw, telemetry = _gateway(fp)

    gw.complete(task="generate", system="s", user="u")

    rec = telemetry.records[-1]
    # request.model is the PRIMARY (resolved before the call, FailoverProvider.model == primary).
    assert rec.model == "deepseek-v4-pro"
    # response.model is the SECONDARY's body model — the provider that actually answered.
    assert rec.response_model == "gpt-4o-mini"
    assert rec.model != rec.response_model


class _FailingProvider:
    def __init__(self, model: str, exc: Exception) -> None:
        self.model = model
        self.exc = exc

    def complete(self, *, system: str, user: str, model: str):
        raise self.exc


# --------------------------------------------------------------------------- P2a: capability probe


class _NativeProvider:
    """Offline structured-tool provider: scripts a sequence of ToolCallResponse turns so the
    native loop can be exercised at $0 (mirrors how agent frameworks test tool loops with a mock
    LLM). Records the messages it was handed so the test can assert the tool-result feedback."""

    supports_tools = True
    model = "deepseek-v4-pro"

    def __init__(self, turns: list[ToolCallResponse]) -> None:
        self.turns = turns
        self.calls = 0
        self.seen_messages: list[list[dict[str, Any]]] = []

    def complete_with_tools(
        self, *, messages: list[dict[str, Any]], tools: list[dict[str, Any]], model: str
    ) -> ToolCallResponse:
        self.seen_messages.append([dict(m) for m in messages])
        turn = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return turn


def test_provider_supports_tools_probe() -> None:
    assert provider_supports_tools(_NativeProvider([ToolCallResponse(text="x")])) is True
    assert provider_supports_tools(_LegacyThreeTupleProvider()) is False
    # supports_tools True but no callable method → still False (must have both).

    class _Half:
        supports_tools = True

    assert provider_supports_tools(_Half()) is False
    # never raises on a weird object
    assert provider_supports_tools(object()) is False


def test_gateway_complete_with_tools_records_telemetry() -> None:
    provider = _NativeProvider([ToolCallResponse(text="done", input_tokens=12, output_tokens=4,
                                                 response_model="deepseek-v4-pro-0613")])
    gw, telemetry = _gateway(provider, task="agent_react")
    resp = gw.complete_with_tools(task="agent_react", messages=[{"role": "user", "content": "g"}],
                                  tools=[])
    assert resp.text == "done"
    rec = telemetry.records[-1]
    assert rec.input_tokens == 12
    assert rec.output_tokens == 4
    assert rec.model == "deepseek-v4-pro"  # request side = configured model
    assert rec.response_model == "deepseek-v4-pro-0613"


def test_gateway_complete_with_tools_guided_error_when_unsupported() -> None:
    gw, _ = _gateway(_LegacyThreeTupleProvider(), task="agent_react")
    with pytest.raises(LLMGatewayError, match="does not support native tool-calling"):
        gw.complete_with_tools(task="agent_react", messages=[], tools=[])


# --------------------------------------------------------------------------- P2a: response parsing


class _FakeFn:
    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _FakeToolCall:
    def __init__(self, call_id: str, name: str, arguments: str) -> None:
        self.id = call_id
        self.function = _FakeFn(name, arguments)


class _FakeMessage:
    def __init__(self, content: Any, tool_calls: Any) -> None:
        self.content = content
        self.tool_calls = tool_calls


class _FakeChoice:
    def __init__(self, message: _FakeMessage) -> None:
        self.message = message


class _FakeUsage:
    prompt_tokens = 7
    completion_tokens = 2
    prompt_cache_hit_tokens = 1


class _FakeResp:
    def __init__(self, message: _FakeMessage, model: str = "deepseek-v4-pro") -> None:
        self.choices = [_FakeChoice(message)]
        self.usage = _FakeUsage()
        self.model = model


def test_parse_tool_response_decodes_tool_calls() -> None:
    resp = _FakeResp(
        _FakeMessage(None, [_FakeToolCall("call_1", "audit_project", '{"x": 1}')])
    )
    parsed = _parse_tool_response(resp)
    assert parsed.wants_tool_calls
    assert parsed.tool_calls == [
        ToolCall(call_id="call_1", name="audit_project", arguments={"x": 1})
    ]
    assert parsed.response_model == "deepseek-v4-pro"
    assert parsed.input_tokens == 7
    assert parsed.cached_input_tokens == 1


def test_parse_tool_response_final_text() -> None:
    parsed = _parse_tool_response(_FakeResp(_FakeMessage("all good", None)))
    assert not parsed.wants_tool_calls
    assert parsed.text == "all good"


def test_parse_tool_response_malformed_arguments_degrade_to_empty() -> None:
    # A broken arguments JSON must not crash the loop — it becomes {} and the registry then
    # surfaces any missing-required-arg error as an observation.
    parsed = _parse_tool_response(
        _FakeResp(_FakeMessage(None, [_FakeToolCall("c", "list_issues", "{not json")]))
    )
    assert parsed.tool_calls[0].arguments == {}


# --------------------------------------------------------------------------- P2a: tool schema


def test_skill_openai_tool_schema_marks_required() -> None:
    skill = Skill(
        name="build_context_pack",
        description="lookup",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=lambda **k: {"ok": True},
        parameters=(
            SkillParameter("query", "string", "what", required=True),
            SkillParameter("budget_tokens", "integer", "size", required=False),
        ),
    )
    schema = skill.openai_tool_schema()
    assert schema["type"] == "function"
    fn = schema["function"]
    assert fn["name"] == "build_context_pack"
    assert fn["parameters"]["properties"]["query"]["type"] == "string"
    assert fn["parameters"]["properties"]["budget_tokens"]["type"] == "integer"
    assert fn["parameters"]["required"] == ["query"]


def test_registry_openai_tools_honours_allowed_filter() -> None:
    reg = SkillRegistry()
    for name in ("audit_project", "list_issues"):
        reg.register(
            Skill(
                name=name,
                description="d",
                cost_tier=CostTier.DETERMINISTIC,
                side_effect=SideEffect.READ_ONLY,
                handler=lambda **k: {},
            )
        )
    all_tools = reg.openai_tools()
    assert {t["function"]["name"] for t in all_tools} == {"audit_project", "list_issues"}
    filtered = reg.openai_tools(allowed={"audit_project"})
    assert {t["function"]["name"] for t in filtered} == {"audit_project"}
