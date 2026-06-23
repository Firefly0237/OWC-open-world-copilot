"""Tree of Thoughts: the generic BFS primitive, the premise value function, and the opt-in premise
search wired into WorldSeedService (offline / $0)."""

from __future__ import annotations

import json
import re

from owcopilot.content.models import ContentBundle
from owcopilot.inspiration import ReferenceContextBuilder
from owcopilot.llm.cache import HashingEmbedder, NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.retrieval.bm25 import BM25Retriever
from owcopilot.retrieval.context_pack import ContextPackBuilder
from owcopilot.storage import SQLiteStore
from owcopilot.worldgen.models import WorldSeedBrief
from owcopilot.worldgen.offline import OfflineWorldSeedProvider
from owcopilot.worldgen.service import WorldSeedService
from owcopilot.worldgen.stages import PREMISE, stage_from_system
from owcopilot.worldgen.tot import LLMPremiseEvaluator, score_premise, tree_of_thoughts


# --------------------------------------------------------------------------- the generic primitive
def test_tot_single_step_is_best_of_n() -> None:
    result = tree_of_thoughts(
        root=None,
        expand=lambda _root: ["x", "yy", "zzz"],
        evaluate=lambda state: (float(len(state)), ""),
        steps=1,
        beam_width=1,
    )
    assert result.best == "zzz"
    assert result.best_score == 3.0
    # All candidates are surfaced, highest score first.
    assert [c.state for c in result.evaluated] == ["zzz", "yy", "x"]


def test_tot_bfs_prunes_to_the_beam_and_finds_the_best_leaf() -> None:
    # 3-ary tree over strings; value = number of 'c's. Best 2-step leaf is "cc".
    result = tree_of_thoughts(
        root="",
        expand=lambda s: [s + "a", s + "b", s + "c"],
        evaluate=lambda s: (float(s.count("c")), s),
        steps=2,
        beam_width=1,
    )
    assert result.best == "cc"
    assert result.best_score == 2.0


def test_tot_beam_width_keeps_multiple_candidates() -> None:
    result = tree_of_thoughts(
        root="",
        expand=lambda s: [s + "a", s + "b"],
        evaluate=lambda s: (float(len(s)), ""),
        steps=1,
        beam_width=2,
    )
    assert len(result.evaluated) == 2


def test_tot_empty_expansion_returns_root() -> None:
    result = tree_of_thoughts(
        root="seed", expand=lambda _s: [], evaluate=lambda _s: (1.0, ""), steps=1
    )
    assert result.best == "seed"
    assert result.evaluated == []


# --------------------------------------------------------------------------- premise value function
def test_score_premise_rewards_a_complete_specific_spine() -> None:
    full = {
        "central_conflict": "对立双方为核心资源此刻摊牌，秩序与自由各执一端，谁退让谁崩塌。" * 2,
        "dramatic_question": "你站哪边？",
        "stakes": "炉心枯竭。",
        "faction_axes": ["集权 vs 自由", "垄断 vs 流通"],
        "themes": ["秩序与自由", "牺牲谁"],
    }
    flat = {"summary": "一个世界", "central_conflict": ""}
    full_score, _ = score_premise(full)
    flat_score, _ = score_premise(flat)
    assert flat_score == 0.0
    assert full_score > flat_score


# --------------------------------------------------------------------------- integration (offline)
def _service(provider, *, premise_candidates: int) -> tuple[WorldSeedService, SQLiteStore]:
    store = SQLiteStore()
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"world_seed": "cheap"}),
        cache=NoOpCache(),
    )
    service = WorldSeedService(
        gateway=gateway,
        bundle=ContentBundle(),
        project_context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        reference_context_builder=ReferenceContextBuilder(store, embedder=HashingEmbedder()),
        premise_candidates=premise_candidates,
    )
    return service, store


def _brief() -> WorldSeedBrief:
    return WorldSeedBrief(
        idea="霜冷山脉边境的能源走私",
        use_references=False,
        use_project_facts=False,
        faction_count=3,
        region_count=2,
        npc_count=3,
        quest_count=2,
        term_count=0,
    )


class _CountingProvider:
    """Wraps the offline double and counts how many times the premise stage was generated."""

    def __init__(self) -> None:
        self.inner = OfflineWorldSeedProvider()
        self.premise_calls = 0

    def complete(self, *, system: str, user: str, model: str):
        if stage_from_system(system) == PREMISE:
            self.premise_calls += 1
        return self.inner.complete(system=system, user=user, model=model)


def test_premise_tot_explores_n_candidates_and_assembles_a_valid_world() -> None:
    provider = _CountingProvider()
    service, store = _service(provider, premise_candidates=3)
    try:
        draft = service.generate(_brief())
        # ToT generated three premise candidates ...
        assert provider.premise_calls == 3
        # ... and the chain still assembled a real world grounded in the chosen spine.
        assert draft.summary
        assert any(e.type.value == "faction" for e in draft.bundle.entities.values())
        assert len(draft.bundle.quests) == 2
    finally:
        store.close()


class _VaryingPremiseProvider:
    """Returns a DIFFERENT premise per ToT variant: higher variant index = more faction axes =
    higher score_premise. So variant 2 (of 0,1,2) is the best and must be the one selected."""

    def __init__(self) -> None:
        self.inner = OfflineWorldSeedProvider()

    def complete(self, *, system: str, user: str, model: str):
        if stage_from_system(system) == PREMISE:
            match = re.search(r"\[PREMISE_VARIANT (\d+)\]", system)
            index = int(match.group(1)) if match else 0
            payload = {
                "summary": f"variant-{index}-world",
                "central_conflict": "对立双方为核心资源此刻摊牌，秩序与自由各执一端。",
                "dramatic_question": "你站哪边？",
                "themes": ["秩序与自由"],
                "faction_axes": ["集权 vs 自由"] * (index + 1),
                "stakes": "炉心枯竭。",
                "style_guide": {"body": "风格", "rules": ["规则"]},
                "terms": [],
                "reference_report": [],
            }
            text = json.dumps(payload, ensure_ascii=False)
            return text, 1, 1
        return self.inner.complete(system=system, user=user, model=model)


def test_premise_tot_selects_the_highest_scoring_candidate() -> None:
    service, store = _service(_VaryingPremiseProvider(), premise_candidates=3)
    try:
        draft = service.generate(_brief())
        # The richest spine (variant 2, most faction axes) won the search.
        assert draft.summary == "variant-2-world"
    finally:
        store.close()


# --------------------------------------------------------------------------- LLM value function
_FULL_PREMISE = {
    "central_conflict": "对立双方为核心资源此刻摊牌，秩序与自由各执一端。",
    "dramatic_question": "你站哪边？",
    "stakes": "炉心枯竭。",
    "faction_axes": ["集权 vs 自由"],
    "themes": ["秩序与自由"],
}


class _ScoreProvider:
    """Returns a fixed JSON rating, as the evaluator's model would."""

    def __init__(self, score, reason: str = "ok") -> None:
        self.score = score
        self.reason = reason

    def complete(self, *, system: str, user: str, model: str):
        return json.dumps({"score": self.score, "reason": self.reason}), 1, 1


def _eval_gateway(provider) -> LLMGateway:
    return LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping={"world_seed": "cheap"}),
        cache=NoOpCache(),
    )


def test_llm_evaluator_adds_rating_on_top_of_the_deterministic_floor() -> None:
    base, _ = score_premise(_FULL_PREMISE)
    evaluate = LLMPremiseEvaluator(_eval_gateway(_ScoreProvider(7.0)), weight=1.0)
    total, rationale = evaluate(_FULL_PREMISE)
    assert total == base + 7.0
    assert "llm=7.0" in rationale


def test_llm_evaluator_clamps_and_degrades_to_deterministic_on_bad_reply() -> None:
    base, _ = score_premise(_FULL_PREMISE)
    # Out-of-range rating is clamped to 10.
    high, _ = LLMPremiseEvaluator(_eval_gateway(_ScoreProvider(99)))(_FULL_PREMISE)
    assert high == base + 10.0
    # An unparsable reply falls back to the deterministic score alone (never crashes).
    bad, rationale = LLMPremiseEvaluator(_eval_gateway(_ScoreProvider("not-a-number")))(
        _FULL_PREMISE
    )
    assert bad == base
    assert "unparsable" in rationale


class _LLMEvalProvider:
    """Premise variants are structurally IDENTICAL (so score_premise ties); only the LLM evaluator
    discriminates, and it rates variant 1 highest. So variant 1 must win — proving the evaluator
    breaks the tie the deterministic score cannot."""

    def __init__(self) -> None:
        self.inner = OfflineWorldSeedProvider()

    def complete(self, *, system: str, user: str, model: str):
        if "PREMISE_VALUE_EVALUATOR" in system:
            match = re.search(r"variant-(\d+)-world", user)
            index = int(match.group(1)) if match else 0
            return json.dumps({"score": 9.0 if index == 1 else 3.0, "reason": f"v{index}"}), 1, 1
        if stage_from_system(system) == PREMISE:
            match = re.search(r"\[PREMISE_VARIANT (\d+)\]", system)
            index = int(match.group(1)) if match else 0
            payload = {
                "summary": f"variant-{index}-world",
                "central_conflict": "对立双方为资源此刻摊牌。",
                "dramatic_question": "你站哪边？",
                "themes": ["秩序与自由"],
                "faction_axes": ["集权 vs 自由"],
                "stakes": "炉心枯竭。",
                "style_guide": {"body": "x", "rules": ["r"]},
                "terms": [],
                "reference_report": [],
            }
            return json.dumps(payload, ensure_ascii=False), 1, 1
        return self.inner.complete(system=system, user=user, model=model)


def test_premise_tot_with_llm_evaluator_breaks_a_structural_tie() -> None:
    provider = _LLMEvalProvider()
    store = SQLiteStore()
    gateway = _eval_gateway(provider)
    service = WorldSeedService(
        gateway=gateway,
        bundle=ContentBundle(),
        project_context_builder=ContextPackBuilder(bm25=BM25Retriever(store)),
        reference_context_builder=ReferenceContextBuilder(store, embedder=HashingEmbedder()),
        premise_candidates=3,
        premise_evaluator=LLMPremiseEvaluator(gateway),
    )
    try:
        draft = service.generate(_brief())
        # Structural scores tie; the LLM rating (variant 1 = 9, others = 3) decides.
        assert draft.summary == "variant-1-world"
    finally:
        store.close()
