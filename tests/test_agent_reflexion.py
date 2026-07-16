"""Reflexion Self-Reflection component (Shinn et al., 2023): :func:`distill_reflection` turns a
failed trial's evaluator feedback into ONE verbal reflection. Verifies the deterministic $0 template
stands in for the Self-Reflection LLM only under the offline opt-in, that the real gateway path is
taken when the opt-in is off, and that a blank model reply fails loudly rather than degrading into
plain retry. The bounded trial loop that consumes these reflections is owned by the multi-agent
single-shot retry and is covered in test_multi_agent.py."""

from __future__ import annotations

import pytest

from owcopilot.agent import ReActAgent, ReflexionError
from owcopilot.agent.reflexion import distill_reflection
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.content.store import ContentStore
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
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


def _gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"agent_react": "cheap"}),
        cache=NoOpCache(),
    )


class _ScriptedReflectionProvider:
    """The online Self-Reflection LLM double: returns a fixed reflection string. Used at $0 with
    OWCOPILOT_ALLOW_OFFLINE_LLM flipped off so distill_reflection takes the real gateway path."""

    def __init__(self, reflection: str) -> None:
        self.reflection = reflection
        self.calls = 0

    def complete(self, *, system: str, user: str, model: str):
        self.calls += 1
        return self.reflection, 1, 1


# --------------------------------------------------------------------------- self-reflection distil
def test_distill_reflection_offline_uses_deterministic_template(tmp_path) -> None:
    # Default env (conftest sets OWCOPILOT_ALLOW_OFFLINE_LLM=1): the $0 template stands in for the
    # Self-Reflection LLM and no gateway call is made.
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedReflectionProvider("SHOULD NOT BE CALLED")
    agent = ReActAgent(gateway=_gateway(provider), registry=registry, max_steps=6)

    reflection = distill_reflection(
        agent=agent, goal="g", trial_index=0, feedback="found 3 open errors"
    )

    assert provider.calls == 0  # offline path made no LLM call
    assert "found 3 open errors" in reflection  # feedback carried verbatim
    assert "第1次" in reflection


def test_distill_reflection_online_calls_self_reflection_llm(tmp_path, monkeypatch) -> None:
    # Flip the offline opt-in off to exercise the real gateway path — still $0 via a scripted fake.
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "0")
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    provider = _ScriptedReflectionProvider("Next time run audit_project before concluding.")
    agent = ReActAgent(gateway=_gateway(provider), registry=registry, max_steps=6)

    reflection = distill_reflection(agent=agent, goal="g", trial_index=0, feedback="had errors")

    assert provider.calls == 1
    assert reflection == "Next time run audit_project before concluding."


def test_distill_reflection_blank_output_raises_reflexion_error(tmp_path, monkeypatch) -> None:
    # No silent downgrade: a blank model reply must raise, not feed an empty reflection forward.
    monkeypatch.setenv("OWCOPILOT_ALLOW_OFFLINE_LLM", "0")
    content_root = tmp_path / "content"
    _dirty_project(content_root)
    registry = default_skill_registry(content_root=str(content_root))
    agent = ReActAgent(
        gateway=_gateway(_ScriptedReflectionProvider("   \n  ")), registry=registry, max_steps=6
    )

    with pytest.raises(ReflexionError):
        distill_reflection(agent=agent, goal="g", trial_index=0, feedback="had errors")
