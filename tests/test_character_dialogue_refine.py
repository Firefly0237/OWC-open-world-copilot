"""critic→refine loop extended to character sheets and dialogue trees (the round-22 quest pattern,
generalised). Offline / $0 — the offline doubles drive both halves of each loop.
"""

from __future__ import annotations

import json

from owcopilot.assist.characters import (
    CharacterBrief,
    CharacterProfileService,
    OfflineCharacterProvider,
)
from owcopilot.assist.critic import CharacterCritic, DialogueCritic
from owcopilot.assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_lin": Entity(
                id="npc_lin", name="林潮生", type=EntityType.NPC, description="领航员。"
            ),
            "npc_mei": Entity(id="npc_mei", name="梅", type=EntityType.NPC, description="掮客。"),
        }
    )


def _gateway(provider: object, task: str) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={task: "cheap"}),
        cache=NoOpCache(),
    )


def test_character_refine_loop_runs_and_converges() -> None:
    store = SQLiteStore()
    try:
        bundle = _bundle()
        store.replace_content_index(bundle)
        gateway = _gateway(OfflineCharacterProvider(), "character_profile")
        draft = CharacterProfileService(
            gateway=gateway,
            bundle=bundle,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            critic=CharacterCritic(gateway=gateway),
            max_refine_rounds=2,
        ).generate(CharacterBrief(name="白盐", concept="以记忆为筹码的掮客。"))
        # offline sheet is complete → critic passes at round 0; the loop ran and recorded it
        assert draft.refine_trail
        assert draft.refine_trail[0].verdict == "pass"
        assert draft.auto_review_incomplete is False
        assert draft.entity.metadata.get("refine_rounds") == len(draft.refine_trail)
    finally:
        store.close()


def test_dialogue_refine_loop_runs_and_converges() -> None:
    bundle = _bundle()
    gateway = _gateway(OfflineDialogueTreeProvider(), "dialogue_tree")
    result = DialogueTreeService(
        gateway=gateway,
        bundle=bundle,
        critic=DialogueCritic(gateway=gateway),
        max_refine_rounds=2,
    ).generate(participant_ids=["npc_lin", "npc_mei"], brief="错音的来源")
    # the canned 4-node tree is well-formed → critic passes; the loop ran
    assert result.refine_trail
    assert result.refine_trail[0].verdict == "pass"
    assert result.auto_review_incomplete is False


class _JunkCharacterCriticProvider:
    """Valid sheet for generation, unparsable text for every critique — proves no fake pass."""

    def complete(self, *, system: str, user: str, model: str) -> tuple[str, int, int]:
        if "reviewing a character sheet" in system:
            return "looks fine, ship it", 10, 10
        return (
            json.dumps({"name": "白盐", "summary": "掮客", "appearance": "a", "personality": "b"}),
            10,
            10,
        )


def test_character_unparsable_critic_flags_for_human_not_silent_pass() -> None:
    store = SQLiteStore()
    try:
        bundle = _bundle()
        store.replace_content_index(bundle)
        gateway = _gateway(_JunkCharacterCriticProvider(), "character_profile")
        draft = CharacterProfileService(
            gateway=gateway,
            bundle=bundle,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            critic=CharacterCritic(gateway=gateway),
            max_refine_rounds=2,
        ).generate(CharacterBrief(name="白盐", concept="掮客。"))
        assert draft.auto_review_incomplete is True
        assert draft.entity.metadata.get("auto_review_incomplete") is True
        assert all(step.verdict != "pass" for step in draft.refine_trail)
    finally:
        store.close()


def test_without_critic_characters_and_dialogue_are_single_shot() -> None:
    store = SQLiteStore()
    try:
        bundle = _bundle()
        store.replace_content_index(bundle)
        char = CharacterProfileService(
            gateway=_gateway(OfflineCharacterProvider(), "character_profile"),
            bundle=bundle,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        ).generate(CharacterBrief(name="白盐", concept="掮客。"))
        assert char.refine_trail == []
        assert "refine_rounds" not in char.entity.metadata

        dlg = DialogueTreeService(
            gateway=_gateway(OfflineDialogueTreeProvider(), "dialogue_tree"),
            bundle=bundle,
        ).generate(participant_ids=["npc_lin"], brief="测试")
        assert dlg.refine_trail == []
    finally:
        store.close()
