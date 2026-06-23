"""Reflexion memory (Shinn et al., 2023): the refine loop accumulates each round's verbal
reflection and feeds the WHOLE history forward, not just the latest round's fixes."""

from __future__ import annotations

from owcopilot.assist.critic import CritiqueDimension, CritiqueResult, QuestCritic
from owcopilot.assist.drafts import QuestDraftService
from owcopilot.assist.offline import OfflineQuestDraftProvider
from owcopilot.assist.refine import (
    run_refine_loop,
    summarize_reflection,
    with_reflection_memory,
)
from owcopilot.audit.default_rules import build_default_rule_registry
from owcopilot.audit.runner import AuditRunner
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore


def _revise(summary: str) -> CritiqueResult:
    return CritiqueResult(
        verdict="revise",
        score=0.4,
        summary=summary,
        dimensions=[
            CritiqueDimension(dimension="craft", severity="blocker", issue="i", fix="补全")
        ],
    )


# --------------------------------------------------------------------------- helpers
def test_with_reflection_memory_empty_is_passthrough() -> None:
    assert with_reflection_memory(["fix"], []) == ["fix"]


def test_with_reflection_memory_prepends_single_marked_entry() -> None:
    out = with_reflection_memory(["fix1"], ["r0", "r1"])
    assert out[0].startswith("[reflexion-memory]")
    assert "r0" in out[0] and "r1" in out[0]
    assert out[1:] == ["fix1"]


def test_summarize_reflection_captures_round_verdict_summary_and_gaps() -> None:
    note = summarize_reflection(0, _revise("太单薄"), ["缺阶段"])
    assert "第1轮" in note
    assert "revise" in note
    assert "太单薄" in note
    assert "缺阶段" in note


# --------------------------------------------------------------------------- the primitive
def test_reflexion_memory_accumulates_and_feeds_the_whole_history_forward() -> None:
    scripts: list[tuple[list[str], CritiqueResult]] = [
        (["缺阶段"], _revise("第一版太单薄")),
        (["缺代价"], _revise("冲突不足")),
        ([], CritiqueResult(verdict="pass", score=0.95, summary="达标")),
    ]
    state = {"round": 0}
    received: list[list[str]] = []

    def assess(_artifact: int) -> tuple[list[str], CritiqueResult]:
        return scripts[state["round"]]

    def regenerate(artifact: int, fixes: list[str]) -> int:
        received.append(fixes)
        state["round"] += 1
        return artifact + 1

    outcome = run_refine_loop(initial=0, max_rounds=5, assess=assess, regenerate=regenerate)

    # Two revise rounds (each regenerates) then a pass (stops).
    assert [step.verdict for step in outcome.trail] == ["revise", "revise", "pass"]
    assert all(step.reflection for step in outcome.trail)
    assert len(received) == 2

    # Round-0 regeneration carries round 0's reflection only.
    assert received[0][0].startswith("[reflexion-memory]")
    assert "第1轮" in received[0][0]
    assert "第2轮" not in received[0][0]
    # Round-1 regeneration carries the ACCUMULATED memory: both round 0 and round 1.
    assert "第1轮" in received[1][0]
    assert "第2轮" in received[1][0]


# --------------------------------------------------------------------------- integration (offline)
def test_quest_draft_loop_surfaces_reflection_and_still_converges() -> None:
    store = SQLiteStore()
    try:
        bundle = ContentBundle(
            entities={
                "npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC),
                "location_northwatch": Entity(
                    id="location_northwatch", name="Northwatch", type=EntityType.LOCATION
                ),
            }
        )
        store.replace_content_index(bundle)
        gateway = LLMGateway(
            providers={"cheap": OfflineQuestDraftProvider()},
            router=StaticRouter(mapping={"quest_draft": "cheap"}),
            cache=NoOpCache(),
        )
        service = QuestDraftService(
            gateway=gateway,
            context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
            audit_runner=AuditRunner(build_default_rule_registry()),
            bundle=bundle,
            critic=QuestCritic(gateway=gateway),
            max_refine_rounds=2,
        )
        result = service.draft_quest("Aldric Northwatch caravan")

        # Threading reflection memory did not break convergence (revise -> pass) ...
        assert [r.verdict for r in result.refine_trail] == ["revise", "pass"]
        # ... and the first (revise) round surfaces a verbal reflection for the human reviewer.
        assert result.refine_trail[0].reflection
        assert "第1轮" in result.refine_trail[0].reflection
    finally:
        store.close()
