"""Reflexion Self-Reflection component (Shinn et al., 2023): :func:`distill_reflection` turns a
failed trial's evaluator feedback into ONE verbal reflection. Verifies the deterministic $0 template
stands in for the Self-Reflection LLM only under the offline opt-in, that the real chat-model path
is taken when the opt-in is off, and that a blank model reply fails loudly rather than degrading
into plain retry. The bounded trial loop that consumes these reflections is owned by the
multi-agent single-shot retry and is covered in test_multi_agent.py."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage
from pydantic import Field

from owcopilot.agent import ReActAgent, ReflexionError
from owcopilot.agent.reflexion import distill_reflection
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.skills import default_skill_registry


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


class _ScriptedReflectionModel(GenericFakeChatModel):
    """The online Self-Reflection LLM double: replies with a fixed reflection string and records
    every call. Used at $0 with OWCOPILOT_ALLOW_OFFLINE_LLM flipped off so distill_reflection
    takes the real chat-model path."""

    seen: list[list[BaseMessage]] = Field(default_factory=list)

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _reflection_model(reflection: str) -> _ScriptedReflectionModel:
    return _ScriptedReflectionModel(messages=iter([AIMessage(content=reflection)]))


# --------------------------------------------------------------------------- self-reflection distil
def test_distill_reflection_offline_uses_deterministic_template(tmp_path) -> None:
    # Default env (conftest sets OWCOPILOT_ALLOW_OFFLINE_LLM=1): the $0 template stands in for the
    # Self-Reflection LLM and no model call is made.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    model = _reflection_model("SHOULD NOT BE CALLED")
    agent = ReActAgent(model=model, registry=registry, max_steps=6)

    reflection = distill_reflection(
        agent=agent, goal="g", trial_index=0, feedback="found 3 open errors"
    )

    assert model.seen == []  # offline path made no LLM call
    assert "found 3 open errors" in reflection  # feedback carried verbatim
    assert "第1次" in reflection


def test_distill_reflection_online_calls_self_reflection_llm(tmp_path, monkeypatch) -> None:
    # Flip the offline opt-in off to exercise the real chat-model path — still $0 via a fake.
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "0")
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    model = _reflection_model("Next time run audit_project before concluding.")
    agent = ReActAgent(model=model, registry=registry, max_steps=6)

    reflection = distill_reflection(agent=agent, goal="g", trial_index=0, feedback="had errors")

    assert len(model.seen) == 1
    assert reflection == "Next time run audit_project before concluding."
    # The distillation prompt carried the goal and the evaluator feedback to the model.
    sent = "\n".join(str(m.text) for m in model.seen[0])
    assert "Goal: g" in sent
    assert "had errors" in sent


def test_distill_reflection_blank_output_raises_reflexion_error(tmp_path, monkeypatch) -> None:
    # No silent downgrade: a blank model reply must raise, not feed an empty reflection forward.
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "0")
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(model=_reflection_model("   \n  "), registry=registry, max_steps=6)

    with pytest.raises(ReflexionError):
        distill_reflection(agent=agent, goal="g", trial_index=0, feedback="had errors")
