"""Skill registry → LangChain StructuredTool bridge (T2-S2).

Pins the bridge contract: tools really execute through ``registry.run`` inside a real
``create_agent`` loop (validation/coercion/WRITES_CANON stay registry-owned), the observation
``content`` is the canonical sorted-JSON dump (byte parity), the structured result rides along as
the ToolMessage ``artifact``, the ``allowed`` filter matches the manifest contract, and an unknown
tool yields ToolNode's guided error observation instead of a crash.
"""

from __future__ import annotations

import re

import pytest
from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.fakes import scripted_chat_model
from owcopilot.skills import (
    CostTier,
    SideEffect,
    Skill,
    SkillError,
    SkillParameter,
    SkillRegistry,
    default_skill_registry,
)
from owcopilot.skills.langchain_tools import as_structured_tools, dumps_observation


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


def _toy_registry() -> SkillRegistry:
    registry = SkillRegistry()
    registry.register(
        Skill(
            name="lookup",
            description="Look up a fact.",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.READ_ONLY,
            handler=lambda query: {"b": 1, "a": query},
            parameters=(SkillParameter("query", "string", "What to look up.", required=True),),
        )
    )
    registry.register(
        Skill(
            name="write_canon",
            description="Write approved canon.",
            cost_tier=CostTier.DETERMINISTIC,
            side_effect=SideEffect.WRITES_CANON,
            handler=lambda: {"wrote": True},
        )
    )
    return registry


def _tool_messages(messages) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def test_tools_execute_real_skill_and_capture_artifact(tmp_path) -> None:
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = create_agent(
        scripted_chat_model([("audit_project", {}), "audited."]),
        as_structured_tools(registry),
    )

    messages = agent.invoke({"messages": [HumanMessage("audit the world")]})["messages"]

    (tool_msg,) = _tool_messages(messages)
    # The audit really ran: the observation is the audit tool's JSON with a real error count.
    match = re.search(r'"open_errors":\s*(\d+)', str(tool_msg.content))
    assert match is not None
    assert int(match.group(1)) >= 1
    # The structured result rides along as the artifact, byte-consistent with the content.
    assert isinstance(tool_msg.artifact, dict)
    assert tool_msg.artifact["open_errors"] == int(match.group(1))
    assert str(tool_msg.content) == dumps_observation(tool_msg.artifact)
    assert messages[-1].content == "audited."


def test_content_is_sorted_json_byte_parity() -> None:
    agent = create_agent(
        scripted_chat_model([("lookup", {"query": "x"}), "done"]),
        as_structured_tools(_toy_registry()),
    )

    messages = agent.invoke({"messages": [HumanMessage("q")]})["messages"]

    (tool_msg,) = _tool_messages(messages)
    # sort_keys parity with the agent loop's observation serialization (NOT insertion order).
    assert str(tool_msg.content) == '{"a": "x", "b": 1}'
    assert tool_msg.artifact == {"b": 1, "a": "x"}


def test_allowed_filters_schema_surface() -> None:
    registry = _toy_registry()
    assert [t.name for t in as_structured_tools(registry)] == ["lookup", "write_canon"]
    assert [t.name for t in as_structured_tools(registry, allowed={"lookup"})] == ["lookup"]
    # Unknown names in allowed are silently ignored — same contract as registry.manifest().
    assert [t.name for t in as_structured_tools(registry, allowed={"lookup", "nope"})] == ["lookup"]


def test_unknown_tool_is_guided_error_observation() -> None:
    agent = create_agent(
        scripted_chat_model([("delete_everything", {}), "recovered"]),
        as_structured_tools(_toy_registry(), allowed={"lookup"}),
    )

    messages = agent.invoke({"messages": [HumanMessage("q")]})["messages"]

    (tool_msg,) = _tool_messages(messages)
    assert tool_msg.status == "error"
    assert "not a valid tool" in str(tool_msg.content)
    assert messages[-1].content == "recovered"


def test_registry_validation_still_raises_skill_error() -> None:
    (lookup, _) = as_structured_tools(_toy_registry())
    # Dict-schema input passes through LangChain unvalidated by design; the registry is the one
    # validation surface and still rejects a missing required argument.
    with pytest.raises(SkillError, match="missing required argument"):
        lookup.invoke({})


def test_registry_coercion_applies_to_dict_schema_input() -> None:
    (lookup, _) = as_structured_tools(_toy_registry())
    content, artifact = lookup.func(query=5)
    # The registry's lenient coercion ran: a stringified scalar reached the handler as "5".
    assert artifact == {"b": 1, "a": "5"}
    assert content == dumps_observation(artifact)


def test_writes_canon_skill_never_auto_invocable() -> None:
    (_, write_canon) = as_structured_tools(_toy_registry())
    with pytest.raises(SkillError, match="cannot be\\s+auto-invoked"):
        write_canon.invoke({})


def test_tool_call_ids_pair_with_tool_messages() -> None:
    agent = create_agent(
        scripted_chat_model(
            [[("lookup", {"query": "a"}), ("lookup", {"query": "b"})], "both done"]
        ),
        as_structured_tools(_toy_registry()),
    )

    messages = agent.invoke({"messages": [HumanMessage("q")]})["messages"]

    (assistant_turn,) = [m for m in messages if isinstance(m, AIMessage) and m.tool_calls]
    requested_ids = [tc["id"] for tc in assistant_turn.tool_calls]
    answered_ids = [m.tool_call_id for m in _tool_messages(messages)]
    assert answered_ids == requested_ids  # results correlate to the requesting call ids, in order
