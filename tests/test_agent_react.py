from __future__ import annotations

import logging
import re
from typing import Any

import pytest

from owcopilot.agent import ReActAgent, parse_react_step
from owcopilot.agent.offline import OfflineGoalAwareReActProvider, OfflineReactProvider
from owcopilot.agent.react import _dumps, _reflections_block, _user_prompt
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway, ToolCall, ToolCallResponse
from owcopilot.llm.router import StaticRouter
from owcopilot.skills import (
    CostTier,
    SideEffect,
    Skill,
    default_skill_registry,
)


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


class _RecordingProvider:
    """Like _ScriptedProvider but records the exact ``user`` prompt of every planning call, so a
    test can assert what context (e.g. reflection memory) the actor was actually handed."""

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.calls = 0
        self.seen_user: list[str] = []

    def complete(self, *, system: str, user: str, model: str):
        self.seen_user.append(user)
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
            ToolCallResponse(
                text="Audit complete; fixes go to the review queue.",
                input_tokens=8,
                output_tokens=6,
            ),
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


def test_native_tools_preserves_thought_and_echoes_text_content(tmp_path) -> None:
    # T3: the model's text alongside its tool_call IS the ReAct thought (Yao et al., 2022) and must
    # be preserved on the step record AND echoed as the assistant message content (OpenAI contract).
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                text="I should audit the project first.",
                tool_calls=[ToolCall(call_id="c1", name="audit_project", arguments={})],
            ),
            ToolCallResponse(text="Audit done."),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=6, use_native_tools=True
    )

    result = agent.run("Get this world ready to export.")

    # Thought captured from the assistant turn's text.
    assert result.steps[0].thought == "I should audit the project first."
    # The assistant echo message carries that text as content (full-message echo per OpenAI).
    echo = next(
        m for m in provider.seen_messages[1] if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert echo["content"] == "I should audit the project first."
    # Behaviour otherwise unchanged.
    assert result.steps[0].action == "audit_project"
    assert not result.steps[0].is_error


def test_native_tools_without_text_keeps_thought_empty_and_content_none(tmp_path) -> None:
    # Backward-compat: when the model returns NO text alongside its tool_call, the step thought
    # stays "" (as before) and the echoed assistant content is byte-identical None (not "") —
    # proving `text or None` preserves the exact prior default.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c1", name="audit_project", arguments={})]
            ),
            ToolCallResponse(text="done"),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=6, use_native_tools=True
    )

    result = agent.run("Get this world ready to export.")

    assert result.steps[0].thought == ""
    echo = next(
        m for m in provider.seen_messages[1] if m.get("role") == "assistant" and m.get("tool_calls")
    )
    assert echo["content"] is None


def test_native_tools_multicall_turn_attaches_text_to_first_step_only(tmp_path) -> None:
    # Documented mapping: a single assistant turn carries one text blob but may request several
    # tool_calls. The text is attached to the FIRST step of the turn only; later steps of the same
    # turn keep thought="" so the reasoning is not duplicated across steps.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                text="Audit then list the issues.",
                tool_calls=[
                    ToolCall(call_id="c1", name="audit_project", arguments={}),
                    ToolCall(call_id="c2", name="list_issues", arguments={}),
                ],
            ),
            ToolCallResponse(text="done"),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=6, use_native_tools=True
    )

    result = agent.run("Audit and list issues.")

    assert [s.action for s in result.steps] == ["audit_project", "list_issues"]
    assert result.steps[0].thought == "Audit then list the issues."  # first step of the turn
    assert result.steps[1].thought == ""  # later step of the same turn: no duplicate reasoning


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


def test_default_prefers_native_but_falls_back_to_text_byte_identical(tmp_path) -> None:
    # New default: use_native_tools=True (prefer native). An OfflineReactProvider has NO native
    # tool support, so the agent transparently runs the exact text trajectory — the $0 suite is
    # driven entirely by such fakes, so its behaviour is unchanged.
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
    assert agent.use_native_tools is True  # prefer-native default
    assert not agent._native_tools_available()  # but the unsupported provider → text path


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


# ------------------------------------------------------- native is the default when supported


def test_native_is_the_default_when_provider_supports_tools(tmp_path) -> None:
    # With use_native_tools omitted (new default True) and a provider that DOES support tools, the
    # agent takes the native function-calling path — no explicit opt-in needed.
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
            ToolCallResponse(text="done", input_tokens=8, output_tokens=6),
        ]
    )
    agent = ReActAgent(gateway=_tool_gateway(provider), registry=registry, max_steps=6)

    result = agent.run("Get this world ready to export.")

    assert agent._native_tools_available()  # default preferred native
    assert provider.calls > 0  # the native complete_with_tools path actually ran
    assert [s.action for s in result.steps] == ["audit_project"]


def test_text_and_native_paths_select_the_same_tools(tmp_path) -> None:
    # Parity: for the same goal, the text ReAct path and the native function-calling path execute
    # the SAME tool selection (both dispatch through the same registry). Guards against the two
    # paths drifting apart now that native is the default.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    goal = "Audit the world and list what's broken."
    gold = ["audit_project", "list_issues"]

    # text path: the goal-aware offline provider emits the gold sequence in the text ReAct format.
    text_agent = ReActAgent(
        gateway=_gateway(OfflineGoalAwareReActProvider.from_scenarios([(goal, gold)])),
        registry=default_skill_registry(content_root=str(content_root)),
        max_steps=6,
        use_native_tools=False,
    )
    text_result = text_agent.run(goal)

    # native path: a scripted tool provider emits the same tools as structured tool_calls.
    native_provider = _ScriptedToolProvider(
        [
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c1", name="audit_project", arguments={})],
                input_tokens=10,
                output_tokens=5,
            ),
            ToolCallResponse(
                tool_calls=[ToolCall(call_id="c2", name="list_issues", arguments={})],
                input_tokens=10,
                output_tokens=5,
            ),
            ToolCallResponse(text="done", input_tokens=8, output_tokens=6),
        ]
    )
    native_agent = ReActAgent(
        gateway=_tool_gateway(native_provider),
        registry=default_skill_registry(content_root=str(content_root)),
        max_steps=6,
    )
    native_result = native_agent.run(goal)

    def executed(result) -> list[str]:
        return [s.action for s in result.steps if s.action and not s.is_error]

    assert executed(text_result) == gold
    assert executed(native_result) == gold
    assert executed(text_result) == executed(native_result)  # parity


# ----------------------------------------------------- Reflexion memory: the `reflections` param


def test_user_prompt_byte_identical_without_reflections() -> None:
    # Backward-compat hard guarantee: default / None / empty reflections must leave the built prompt
    # byte-for-byte unchanged versus the pre-Reflexion signature.
    transcript = ["Thought: t\nAction: audit_project\nAction Input: {}\nObservation: {}"]
    base = _user_prompt("goal", transcript, 2500)
    assert _user_prompt("goal", transcript, 2500, reflections=None) == base
    assert _user_prompt("goal", transcript, 2500, reflections=[]) == base
    # Same for the empty-transcript (Begin) branch.
    base_begin = _user_prompt("goal", [])
    assert _user_prompt("goal", [], reflections=None) == base_begin
    assert _user_prompt("goal", [], reflections=[]) == base_begin


def test_user_prompt_renders_reflection_block_before_transcript() -> None:
    prompt = _user_prompt("goal", [], reflections=["ran no audit", "wrong tool"])
    assert _reflections_block(["ran no audit", "wrong tool"]) in prompt
    assert "- ran no audit" in prompt
    assert "- wrong tool" in prompt
    # Block sits after the Goal header and before the Begin/transcript section.
    assert prompt.index("Goal: goal") < prompt.index("Reflections from previous attempts:")
    assert prompt.index("Reflections from previous attempts:") < prompt.index("Begin.")


def test_run_text_threads_reflections_into_actor_prompt(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _RecordingProvider(["Thought: done\nFinal Answer: report"])
    agent = ReActAgent(
        gateway=_gateway(provider), registry=registry, max_steps=6, use_native_tools=False
    )

    agent.run("diagnose", reflections=["last time I skipped audit_project"])

    assert "Reflections from previous attempts:" in provider.seen_user[0]
    assert "- last time I skipped audit_project" in provider.seen_user[0]


def test_run_native_threads_reflections_into_messages_and_default_is_byte_identical(
    tmp_path,
) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))

    # Default (no reflections): the first turn's messages are exactly [system, "Goal:"] — unchanged.
    plain = _ScriptedToolProvider([ToolCallResponse(text="done")])
    ReActAgent(gateway=_tool_gateway(plain), registry=registry, max_steps=3).run("g")
    first_plain = plain.seen_messages[0]
    assert [m["role"] for m in first_plain] == ["system", "user"]
    assert first_plain[1]["content"] == "Goal: g"

    # With reflections: exactly one extra user message carrying the shared reflection block.
    withref = _ScriptedToolProvider([ToolCallResponse(text="done")])
    ReActAgent(gateway=_tool_gateway(withref), registry=registry, max_steps=3).run(
        "g", reflections=["missed the audit step"]
    )
    first_ref = withref.seen_messages[0]
    assert [m["role"] for m in first_ref] == ["system", "user", "user"]
    assert first_ref[2]["content"] == _reflections_block(["missed the audit step"])


# ------------------------- T4: recoverable observation truncation + read_observation round-trip


def _big_blob_skill(blob: str, name: str = "big_blob") -> Skill:
    """A local skill whose result is deliberately larger than the observation char limit, so the
    loop must truncate it and stash the full text in the run-scoped overflow store."""
    return Skill(
        name=name,
        description="Returns an oversized blob (for truncation tests).",
        cost_tier=CostTier.DETERMINISTIC,
        side_effect=SideEffect.READ_ONLY,
        handler=lambda **kw: {"blob": blob},
        parameters=(),
    )


def test_oversized_observation_marker_carries_obs_id_and_stores_full(tmp_path) -> None:
    # TEXT path: an oversized observation is truncated with a guided marker that names the exact
    # read_observation call, the FULL text is stored under obs_id, and a follow-up read_observation
    # restores it byte-for-byte via the real registry + ContextVar. Core acceptance round-trip.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    blob = "x" * 9000
    registry.register(_big_blob_skill(blob))
    provider = _ScriptedProvider(
        [
            "Thought: get big data\nAction: big_blob\nAction Input: {}",
            'Thought: read the rest\nAction: read_observation\nAction Input: {"obs_id": "obs-0-0"}',
            "Thought: done\nFinal Answer: reported",
        ]
    )
    agent = ReActAgent(
        gateway=_gateway(provider), registry=registry, max_steps=6, use_native_tools=False
    )

    result = agent.run("fetch and restore")

    full = _dumps({"blob": blob})
    assert result.steps[0].is_truncated is True
    marker = result.steps[0].observation
    assert 'read_observation(obs_id="obs-0-0")' in marker
    assert "truncated" in marker
    assert "obs-0-0" in marker
    assert agent._overflow["obs-0-0"] == full  # full original stored, recoverable
    # The read_observation step restored the exact original from the structured result.
    assert result.steps[1].action == "read_observation"
    assert not result.steps[1].is_error
    assert result.steps[1].result is not None
    assert result.steps[1].result["content"] == full


def test_oversized_observation_marker_native_path(tmp_path) -> None:
    # NATIVE path parity: same overflow store + guided marker + round-trip through the
    # function-calling loop.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    blob = "x" * 9000
    registry.register(_big_blob_skill(blob))
    provider = _ScriptedToolProvider(
        [
            ToolCallResponse(tool_calls=[ToolCall(call_id="c1", name="big_blob", arguments={})]),
            ToolCallResponse(
                tool_calls=[
                    ToolCall(
                        call_id="c2",
                        name="read_observation",
                        arguments={"obs_id": "obs-0-0"},
                    )
                ]
            ),
            ToolCallResponse(text="done"),
        ]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=6, use_native_tools=True
    )

    result = agent.run("fetch and restore natively")

    full = _dumps({"blob": blob})
    assert result.steps[0].is_truncated is True
    marker = result.steps[0].observation
    assert 'read_observation(obs_id="obs-0-0")' in marker
    assert "read_observation" in marker
    assert agent._overflow["obs-0-0"] == full
    read_step = result.steps[1]
    assert read_step.action == "read_observation"
    assert not read_step.is_error
    assert read_step.result is not None
    assert read_step.result["content"] == full


def test_observation_within_limit_is_byte_identical(tmp_path) -> None:
    # BACK-COMPAT (byte-level): the canonical small-project trajectory has every observation under
    # the limit, so NOTHING is truncated, NOTHING is stored, and each non-error observation is
    # exactly _dumps(result) — no marker/bytes appended. Proves the untruncated path is unchanged.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=6)

    result = agent.run("Get this world ready to export.")

    assert result.stop_reason == "finished"
    assert agent._overflow == {}  # nothing overflowed
    for step in result.steps:
        assert step.is_truncated is False
        if not step.is_error:
            assert step.observation == _dumps(step.result)


def test_obs_ids_unique_across_multiple_truncations(tmp_path) -> None:
    # Two oversized observations across two steps get distinct, collision-free obs_ids: the seq
    # component (count of tails stored so far) disambiguates even within the same step index.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    registry.register(_big_blob_skill("x" * 9000))
    provider = _ScriptedProvider(
        [
            "Thought: big 1\nAction: big_blob\nAction Input: {}",
            "Thought: big 2\nAction: big_blob\nAction Input: {}",
            "Thought: done\nFinal Answer: reported",
        ]
    )
    agent = ReActAgent(
        gateway=_gateway(provider), registry=registry, max_steps=6, use_native_tools=False
    )

    agent.run("two big fetches")

    assert len(agent._overflow) == 2
    assert set(agent._overflow) == {"obs-0-0", "obs-1-1"}


# ------------------------------------------------------------- early_stopping="generate" (opt-in)


def test_early_stopping_defaults_to_force_and_rejects_bad_values(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(gateway=_gateway(OfflineReactProvider()), registry=registry, max_steps=6)
    assert agent.early_stopping == "force"

    with pytest.raises(ValueError, match="early_stopping"):
        ReActAgent(
            gateway=_gateway(OfflineReactProvider()),
            registry=registry,
            max_steps=6,
            early_stopping="bogus",  # type: ignore[arg-type]
        )


def test_force_early_stopping_is_byte_identical_to_default(tmp_path) -> None:
    # Explicit early_stopping="force" must produce the exact same result as omitting it — the
    # default must not have silently changed.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    turns = [
        "Thought: t1\nAction: audit_project\nAction Input: {}",
        "Thought: t2\nAction: audit_project\nAction Input: {}",
    ]
    default_agent = ReActAgent(
        gateway=_gateway(_ScriptedProvider(list(turns))),
        registry=registry,
        max_steps=2,
        use_native_tools=False,
    )
    forced_agent = ReActAgent(
        gateway=_gateway(_ScriptedProvider(list(turns))),
        registry=registry,
        max_steps=2,
        early_stopping="force",
        use_native_tools=False,
    )

    default_result = default_agent.run("byte identical check")
    forced_result = forced_agent.run("byte identical check")

    assert default_result.stop_reason == forced_result.stop_reason == "max_steps"
    assert default_result.final_answer == forced_result.final_answer
    assert "Reached the step budget before finishing" in default_result.final_answer


def test_generate_early_stopping_spends_one_extra_call_and_uses_its_answer(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedProvider(
        [
            "Thought: t1\nAction: audit_project\nAction Input: {}",
            "Thought: t2\nAction: audit_project\nAction Input: {}",
            "Thought: wrapping up\nFinal Answer: Budget used, but the audit found real issues.",
        ]
    )
    agent = ReActAgent(
        gateway=_gateway(provider),
        registry=registry,
        max_steps=2,
        early_stopping="generate",
        use_native_tools=False,
    )

    result = agent.run("test generate finish")

    assert result.stop_reason == "max_steps_generate"
    assert result.final_answer == "Budget used, but the audit found real issues."
    assert result.step_count == 2  # the finish call is a planning call, not a tool step
    assert provider.calls == 3  # 2 normal planning turns + exactly 1 finish call


class _FinishFailsProvider:
    """Answers the first `len(turns)` calls from the script, then raises — used to prove the
    generate-mode finish call's own failure falls back to the 'force' stop honestly."""

    def __init__(self, turns: list[str]) -> None:
        self.turns = turns
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        if self.calls <= len(self.turns):
            return self.turns[self.calls - 1], 1, 1
        raise RuntimeError("finish call boom")


def test_generate_early_stopping_falls_back_to_force_when_finish_call_fails(
    tmp_path, caplog
) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _FinishFailsProvider(
        [
            "Thought: t1\nAction: audit_project\nAction Input: {}",
            "Thought: t2\nAction: audit_project\nAction Input: {}",
        ]
    )
    agent = ReActAgent(
        gateway=_gateway(provider),
        registry=registry,
        max_steps=2,
        early_stopping="generate",
        use_native_tools=False,
    )

    with caplog.at_level(logging.WARNING, logger="owcopilot.agent.react"):
        result = agent.run("test generate finish failure")

    assert result.stop_reason == "max_steps"  # honest fallback, never crashes
    assert result.final_answer == (
        "Reached the step budget before finishing. "
        "Latest findings are in the steps above; "
        "re-run with a higher --max-steps to continue."
    )
    assert provider.calls == 3  # 2 normal calls + 1 failed finish attempt
    assert any("finish call failed" in r.message for r in caplog.records)


class _ToolProviderWithFinish:
    """Native-tools double: `complete_with_tools` always requests a tool (never finishes on its
    own), and `complete` (used only by the generate-mode finish call) returns a scripted answer.
    Calling `complete` without `finish_text` configured is treated as a test failure — it proves
    the main native loop never uses the plain-text path."""

    supports_tools = True
    model = "deepseek-v4-pro"

    def __init__(
        self, tool_turns: list[ToolCallResponse], *, finish_text: str | None = None
    ) -> None:
        self.tool_turns = tool_turns
        self.calls = 0
        self.finish_calls = 0
        self.finish_text = finish_text

    def complete_with_tools(self, *, messages, tools, model: str) -> ToolCallResponse:
        turn = self.tool_turns[min(self.calls, len(self.tool_turns) - 1)]
        self.calls += 1
        return turn

    def complete(self, *, system: str, user: str, model: str):
        self.finish_calls += 1
        assert self.finish_text is not None, "complete() called without a configured finish_text"
        return self.finish_text, 1, 1


def test_native_generate_early_stopping_spends_one_extra_plain_call(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ToolProviderWithFinish(
        [ToolCallResponse(tool_calls=[ToolCall(call_id="c", name="audit_project", arguments={})])],
        finish_text="Thought: wrap\nFinal Answer: native budget exhausted, findings above.",
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider),
        registry=registry,
        max_steps=2,
        early_stopping="generate",
        use_native_tools=True,
    )

    result = agent.run("loop forever, native")

    assert result.stop_reason == "max_steps_generate"
    assert result.final_answer == "native budget exhausted, findings above."
    assert result.step_count == 2
    assert provider.finish_calls == 1  # exactly one extra plain-text finish call
    assert provider.calls == 2  # main loop still ran exactly max_steps native turns


def test_native_force_early_stopping_never_calls_plain_complete(tmp_path) -> None:
    # Default force: the native loop's budget exhaustion must NOT touch complete() at all — the
    # _ToolProviderWithFinish fake asserts internally if it is (finish_text=None).
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ToolProviderWithFinish(
        [ToolCallResponse(tool_calls=[ToolCall(call_id="c", name="audit_project", arguments={})])]
    )
    agent = ReActAgent(
        gateway=_tool_gateway(provider), registry=registry, max_steps=2, use_native_tools=True
    )

    result = agent.run("loop forever, native, force")

    assert result.stop_reason == "max_steps"
    assert provider.finish_calls == 0
