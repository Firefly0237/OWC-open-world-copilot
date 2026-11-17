"""Offline LangChain chat-model fakes driving the real ``create_agent`` loop (T2-S2).

The load-bearing assertion: the transcript-aware fake's final answer quotes the *real*
``open_errors`` count scraped from the tool results it was handed — proof the loop feeds tool
output back into the model, not just that a script replays. The goal-aware fake pins the eval
harness semantics (expected action sequence replayed exactly, real registry execution, fallback
for unknown goals).
"""

from __future__ import annotations

import re

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from owcopilot.agent.offline import OfflineGoalAwareChatModel, OfflineReactChatModel
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.skills import default_skill_registry
from owcopilot.skills.langchain_tools import as_structured_tools


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


def _agent(model, tmp_path):
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(
        content_root=str(content_root), sqlite_path=str(tmp_path / "owc.db")
    )
    return create_agent(model, as_structured_tools(registry))


def _called_tools(messages) -> list[str]:
    return [tc["name"] for m in messages if isinstance(m, AIMessage) for tc in m.tool_calls]


def test_transcript_aware_fake_grounds_final_answer_in_observations(tmp_path) -> None:
    agent = _agent(OfflineReactChatModel(), tmp_path)

    messages = agent.invoke({"messages": [HumanMessage("Get this world ready to export.")]})[
        "messages"
    ]

    assert _called_tools(messages) == ["audit_project", "build_context_pack", "quality_harness"]
    tool_messages = [m for m in messages if isinstance(m, ToolMessage)]
    assert all(m.status != "error" for m in tool_messages)

    # The final answer must quote the audit's real open-error count — the feedback-loop proof.
    match = re.search(r'"open_errors":\s*(\d+)', str(tool_messages[0].content))
    assert match is not None
    open_errors = int(match.group(1))
    assert open_errors >= 1
    final = messages[-1]
    assert isinstance(final, AIMessage) and not final.tool_calls
    assert str(open_errors) in final.text
    # And the harness step really ran (its observation carries the project phase).
    assert "phase" in str(tool_messages[2].content)


def test_transcript_aware_fake_turns_carry_thoughts() -> None:
    model = OfflineReactChatModel()
    first = model.invoke([HumanMessage("go")])
    assert first.tool_calls[0]["name"] == "audit_project"
    assert first.tool_calls[0]["id"] == "call_0"
    assert "consistency audit" in str(first.content)  # the thought rides on the same turn


def test_goal_aware_fake_replays_scripted_actions(tmp_path) -> None:
    model = OfflineGoalAwareChatModel.from_scenarios(
        [("列出所有问题", ["audit_project", "list_issues"])]
    )
    agent = _agent(model, tmp_path)

    messages = agent.invoke({"messages": [HumanMessage("Goal: 列出所有问题")]})["messages"]

    assert _called_tools(messages) == ["audit_project", "list_issues"]
    assert all(m.status != "error" for m in messages if isinstance(m, ToolMessage))
    assert messages[-1].text == "已依序执行 audit_project→list_issues，完成目标。"


def test_goal_aware_fake_reads_goal_without_header_too(tmp_path) -> None:
    model = OfflineGoalAwareChatModel.from_scenarios([("审计一下", ["audit_project"])])
    agent = _agent(model, tmp_path)

    messages = agent.invoke({"messages": [HumanMessage("审计一下")]})["messages"]

    assert _called_tools(messages) == ["audit_project"]
    assert messages[-1].text == "已依序执行 audit_project，完成目标。"


def test_goal_aware_fake_falls_back_for_unknown_goal(tmp_path) -> None:
    model = OfflineGoalAwareChatModel(scripts={})
    agent = _agent(model, tmp_path)

    messages = agent.invoke({"messages": [HumanMessage("Goal: 未收录的目标")]})["messages"]

    assert _called_tools(messages) == ["audit_project"]
    assert messages[-1].text == "审计完毕。"
