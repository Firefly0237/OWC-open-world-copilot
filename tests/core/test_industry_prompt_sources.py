"""Industry-researched source maps are part of every subjective game-content prompt."""

from __future__ import annotations

import json

import pytest

from owcopilot.assist import barks, characters, dialogue_trees, drafts, flavor, industry
from owcopilot.assist.critic import (
    BarkCritic,
    CharacterCritic,
    DialogueCritic,
    FlavorCritic,
    QuestCritic,
)
from owcopilot.assist.voice import VoiceCard
from owcopilot.content.models import Quest
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.logic import draft as logic_draft
from owcopilot.retrieval.models import ContextPack
from owcopilot.worldgen import critic as world_critic
from owcopilot.worldgen import expand as world_expand
from owcopilot.worldgen import service as world_seed


def _assert_sources(text: str, source_ids: tuple[str, ...]) -> None:
    assert "INDUSTRY SOURCE MAP" in text
    for source_id in source_ids:
        assert f"[{source_id}]" in text


def test_source_block_rejects_unknown_source_ids() -> None:
    with pytest.raises(ValueError, match="unknown industry source id"):
        industry.industry_source_block("NOT_A_REAL_SOURCE")


def test_every_source_id_has_a_research_url() -> None:
    assert set(industry.SOURCE_URLS) == set(industry.SOURCE_NOTES)
    assert all(url.startswith("https://") for url in industry.SOURCE_URLS.values())


class _CaptureCriticProvider:
    def __init__(self) -> None:
        self.systems: list[str] = []

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        self.systems.append(system)
        return json.dumps({"verdict": "pass", "score": 1.0, "dimensions": []}), 1, 1


def _gateway(provider: object) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(
            mapping={
                "quest_critique": "cheap",
                "character_profile": "cheap",
                "dialogue_tree": "cheap",
                "barks_batch": "cheap",
                "flavor_batch": "cheap",
            }
        ),
        cache=NoOpCache(),
    )


def test_critic_prompts_embed_researched_source_ids() -> None:
    provider = _CaptureCriticProvider()
    gateway = _gateway(provider)

    QuestCritic(gateway=gateway).critique(
        brief="caravan trouble",
        quest=Quest(id="q1", title="Missing Caravan"),
        context_lines=[],
        readiness_missing=[],
    )
    CharacterCritic(gateway=gateway).critique(
        concept="a conflicted broker",
        profile={"voice": "precise"},
        summary="broker",
        context_lines=[],
        missing_sections=[],
    )
    DialogueCritic(gateway=gateway).critique(
        brief="argument at the dock",
        nodes={},
        speaker_ids=["npc_a", "npc_b"],
        structure_problems=[],
    )
    BarkCritic(gateway=gateway).critique(
        topic="storm warning",
        voice_card_json="{}",
        variants=["Wind is turning."],
        lint_problems=[],
    )
    FlavorCritic(gateway=gateway).critique(
        category="item",
        theme="salt harbor",
        style_text="spare",
        entries=[{"name": "Salt Charm", "description": "protects", "flavor": "old brine"}],
        lint_problems=[],
    )

    expected = [
        industry.QUEST_RUBRIC_SOURCES,
        industry.CHARACTER_RUBRIC_SOURCES,
        industry.DIALOGUE_RUBRIC_SOURCES,
        industry.BARK_RUBRIC_SOURCES,
        industry.FLAVOR_RUBRIC_SOURCES,
    ]
    assert len(provider.systems) == len(expected)
    for prompt, source_ids in zip(provider.systems, expected, strict=True):
        _assert_sources(prompt, source_ids)


def test_generation_quality_prompts_embed_researched_source_ids() -> None:
    prompts = [
        (
            drafts._system_prompt(ContextPack(query="", budget_tokens=0), brief="brief"),
            industry.QUEST_RUBRIC_SOURCES,
        ),
        (characters._SYSTEM_PROMPT, industry.CHARACTER_RUBRIC_SOURCES),
        (dialogue_trees._SYSTEM_PROMPT, industry.DIALOGUE_RUBRIC_SOURCES),
        (
            barks._system_prompt(VoiceCard(entity_id="npc_a", name="A"), max_chars=40),
            industry.BARK_RUBRIC_SOURCES,
        ),
        (flavor._SYSTEM_PROMPT, industry.FLAVOR_RUBRIC_SOURCES),
        (logic_draft._SYSTEM, industry.LOGIC_RUBRIC_SOURCES),
        (world_seed._ROLE, industry.WORLD_RUBRIC_SOURCES),
        (world_expand._ROLE, industry.WORLD_RUBRIC_SOURCES),
        (world_critic._critic_system_prompt(), industry.QUEST_RUBRIC_SOURCES),
    ]
    for prompt, source_ids in prompts:
        _assert_sources(prompt, source_ids)
