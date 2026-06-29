from __future__ import annotations

import re
from typing import Any

from owcopilot.agent import ReActAgent, parse_react_step
from owcopilot.agent.offline import OfflineReactProvider
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.core.skills import default_skill_registry
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, ToolCall, ToolCallResponse
from owcopilot.llm.router import StaticRouter


def _dirty_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            },
            quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")},
        )
    )


def _gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap"}),
        cache=NoOpCache(),
    )


class _ScriptedProvider:
    """Returns the next canned ReAct turn on each call (for testing specific loop behaviours)."""

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        text = self.turns[min(self.calls, len(self.turns) - 1)]
        self.calls += 1
        return text, 1, 1


# --------------------------------------------------------------------------- parser
def test_parse_action_step() -> None:
    step = parse_react_step(
        'Thought: look it up\nAction: build_context_pack\nAction Input: {"query": "Aldric"}'
    )
    assert step.thought == "look it up"
    assert step.action == "build_context_pack"
    assert step.action_input == {"query": "Aldric"}
    assert step.final_answer is None


def test_parse_final_answer() -> None:
    step = parse_react_step("Thought: done\nFinal Answer: all good")
    assert step.final_answer == "all good"
    assert step.action is None


def test_parse_ignores_hallucinated_observation() -> None:
    # The model must not author observations; anything from "Observation:" on is discarded.
    step = parse_react_step(
        "Thought: t\nAction: audit_project\nAction Input: {}\n"
        'Observation: {"open_errors": 99}\nThought: fake'
    )
    assert step.action == "audit_project"
    assert step.action_input == {}


def test_parse_tolerates_fenced_json_and_backticked_name() -> None:
    step = parse_react_step(
        'Thought: t\nAction: `propose_fix`\nAction Input: ```json\n{"issue_id": "x"}\n```'
    )
    assert step.action == "propose_fix"
    assert step.action_input == {"issue_id": "x"}


def test_parse_no_action_no_final() -> None:
    step = parse_react_step("Thought: I am unsure what to do.")
    assert step.action is None
    assert step.final_answer is None


# --- loop: offline reasoning double + real skills ---
def test_agent_runs_canonical_trajectory_and_grounds_answer_in_observations(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=6)

    result = agent.run("Get this world ready to export.")

    assert result.stop_reason == "finished"
    assert [s.action for s in result.steps] == [
        "audit_project",
        "build_context_pack",
        "quality_harness",
    ]
    # Every tool call produced a real observation and none errored.
    assert all(step.observation and not step.is_error for step in result.steps)
    assert result.step_count == 3

    # The final answer must quote the audit's real open-error count — proof the loop fed the
    # tool observation back to the model rather than replaying a fixed script. (The observation is
    # the tool's JSON, possibly truncated for context budget, so read the count with a regex.)
    match = re.search(r'"open_errors":\s*(\d+)', result.steps[0].observation)
    assert match is not None
    open_errors = int(match.group(1))
    assert open_errors >= 1
    assert str(open_errors) in result.final_answer
    # And the harness step really ran (its observation carries the project phase).
    assert "phase" in result.steps[2].observation


def test_agent_stops_at_step_budget(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=2)

    result = agent.run("Diagnose the world.")

    assert result.stop_reason == "max_steps"
    assert result.step_count == 2


def test_agent_reports_unknown_skill_as_recoverable_observation(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedProvider(
        [
            "Thought: try a bad tool\nAction: delete_everything\nAction Input: {}",
            "Thought: ok, audit instead\nAction: audit_project\nAction Input: {}",
            "Thought: done\nFinal Answer: reported",
        ]
    )
    agent = ReActAgent(gateway=_gateway(provider), registry=registry, max_steps=5)

    result = agent.run("test recovery")

    assert result.stop_reason == "finished"
    assert result.steps[0].is_error
    assert "unknown skill" in result.steps[0].observation
    assert result.steps[1].action == "audit_project"
    assert not result.steps[1].is_error


def test_agent_handles_a_turn_with_no_action(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedProvider(
        [
            "Thought: I'm just thinking out loud with no tool.",
            "Thought: now I'll finish\nFinal Answer: ok",
        ]
    )
    agent = ReActAgent(gateway=_gateway(provider), registry=registry, max_steps=5)

    result = agent.run("test no-action nudge")

    assert result.stop_reason == "finished"
    assert result.steps[0].is_error
    assert "No Action or Final Answer" in result.steps[0].observation


# ------------------------------------------------------------------- P2a native tool-calling


class _ScriptedToolProvider:
    """Offline native-function-calling double: returns scripted ToolCallResponse turns so the
    structured ReAct loop runs at $0. Mirrors how agent frameworks unit-test tool loops."""

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


def _tool_gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap"}),
        cache=NoOpCache(),
    )


def test_native_tools_loop_executes_and_feeds_results_back(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c1", name="audit_project", arguments={})],
                input_tokens=10,
                output_tokens=5,
            ),
            ToolCallResponse(text="Audit complete; fixes go to the review queue.",
                             input_tokens=8, output_tokens=6),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=6, use_native_tools=True
    )

    result = agent.run("Get this world ready to export.")

    assert result.stop_reason == "finished"
    assert [s.action for s in result.steps] == ["audit_project"]
    assert not result.steps[0].is_error
    # The structured result was captured and the observation really came from the deterministic tool
    assert result.steps[0].result is not None
    assert "open_errors" in result.steps[0].observation
    # The second planning turn was handed the tool result as a role=tool message (feedback loop).
    second_turn_msgs = provider.seen_messages[1]
    assert any(m.get("role") == "tool" and m.get("tool_call_id") == "c1" for m in second_turn_msgs)
    # And the assistant tool-call echo precedes it (OpenAI contract).
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second_turn_msgs)


def test_native_tools_falls_back_to_text_when_provider_unsupported(tmp_path) -> None:
    # use_native_tools=True but the provider has no native support → the agent must transparently
    # run the canonical TEXT loop instead (OfflineReactProvider drives the standard trajectory).
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(
        gateway=_gateway(OfflineReactProvider()),
        registry=registry,
        max_steps=6,
        use_native_tools=True,  # requested, but provider doesn't support it
    )

    result = agent.run("Get this world ready to export.")

    assert result.stop_reason == "finished"
    # The text trajectory ran — proof we fell back, not errored.
    assert [s.action for s in result.steps] == [
        "audit_project",
        "build_context_pack",
        "quality_harness",
    ]


def test_native_tools_default_false_is_byte_identical_to_text_path(tmp_path) -> None:
    # Default (use_native_tools omitted) must be the exact text trajectory — zero regression.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=6)

    result = agent.run("Get this world ready to export.")

    assert result.stop_reason == "finished"
    assert [s.action for s in result.steps] == [
        "audit_project",
        "build_context_pack",
        "quality_harness",
    ]
    assert agent.use_native_tools is False


def test_native_tools_unknown_skill_is_recoverable_observation(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c1", name="delete_everything", arguments={})]
            ),
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c2", name="audit_project", arguments={})]
            ),
            ToolCallResponse(text="reported"),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=5, use_native_tools=True
    )

    result = agent.run("test native recovery")

    assert result.stop_reason == "finished"
    assert result.steps[0].is_error
    assert "unknown skill" in result.steps[0].observation
    assert result.steps[1].action == "audit_project"
    assert not result.steps[1].is_error


def test_native_tools_step_budget(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    # Model keeps calling a tool, never finishing → must stop at the budget.
    provider = _ScriptedToolProvider(
        [ToolCallResponse(tool_calls=[ToolCall(call_id="c", name="audit_project", arguments={})])]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=2, use_native_tools=True
    )

    result = agent.run("loop forever")

    assert result.stop_reason == "max_steps"
    assert result.step_count == 2
