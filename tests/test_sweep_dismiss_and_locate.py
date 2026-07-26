"""Station-5 "contained" surface: sentence-level localization + per-finding 逐一排除.

Two structural gaps the sweep had for the style/element review scenario:
  * a finding located only to the OBJECT (a whole dialogue tree shared one ref);
  * no way to dismiss a single match so a re-run stops re-surfacing it.

These tests exercise both purely at the service/store level ($0, no network): term/graph/semantic
layers are deterministic, and the LLM judge uses the offline double.
"""

from __future__ import annotations

import pytest

from owcopilot.assist.sweep import (
    _OBJECT_KEY,
    OfflineSweepJudgeProvider,
    SweepExclusionStore,
    ThemeSweepService,
    _finding_id,
    render_sweep_markdown,
)
from owcopilot.content.models import (
    ContentBundle,
    DialogueChoice,
    DialogueNode,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    Relation,
    Term,
)
from owcopilot.llm.cache import NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector


def _sweep_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_dicer": Entity(
                id="npc_dicer",
                name="掷骰人",
                type=EntityType.NPC,
                description="赌坊里讨生活的老千。",
            ),
            "npc_owner": Entity(
                id="npc_owner",
                name="金三爷",
                type=EntityType.NPC,
                description="码头一带的地下钱庄主人。",  # no direct term — graph/semantic flag it
            ),
            "npc_clean": Entity(
                id="npc_clean", name="采药童", type=EntityType.NPC, description="山里采药。"
            ),
        },
        relations=[Relation(source="npc_owner", target="npc_dicer", kind="employs")],
        quests={
            "quest_debt": Quest(id="quest_debt", title="赌债风波", objective="替船工抹平一笔赌债。")
        },
        terms={"term_house": Term(id="term_house", canonical="天和赌坊", description="城南赌坊。")},
    )


class _StubSemanticEmbedder:
    """Deterministic semantic stand-in (model_id ``st:*`` so the sweep treats it as real). Maps any
    gambling-flavoured text to one axis and everything else to an orthogonal one."""

    model_id = "st:stub"
    _GAMBLE = ("赌", "骰", "押", "钱庄", "博彩", "输赢")

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if any(k in t for k in self._GAMBLE) else [0.0, 1.0] for t in texts]


def _offline_judge_gateway() -> LLMGateway:
    return LLMGateway(
        providers={"cheap": OfflineSweepJudgeProvider()},
        router=StaticRouter(mapping={"theme_sweep": "cheap"}),
        cache=NoOpCache(),
        telemetry=TelemetryCollector(),
    )


# --------------------------------------------------------------- sentence-level localization (A1)
def test_sentence_level_locates_to_dialogue_node() -> None:
    # A whole dialogue tree used to be ONE ref. Now each matched node/sentence is its own finding
    # with a node-level locator, while ``ref`` stays object-level (backward compatible).
    bundle = ContentBundle(
        dialogue_trees={
            "tree_x": DialogueTree(
                id="tree_x",
                title="夜谈",  # neutral title — keep the test focused on the node text
                participants=["npc1"],
                root_node="n1",
                nodes={
                    "n1": DialogueNode(
                        id="n1",
                        speaker_id="npc1",
                        text="今晚押大还是押小？",
                        choices=[DialogueChoice(text="不玩了。", next_node="n2")],
                    ),
                    "n2": DialogueNode(
                        id="n2", speaker_id="npc1", text="老张昨天把赌注全输光了。"
                    ),
                },
            )
        }
    )
    report = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    tree = [f for f in report.hits if f.ref == "dialogue_tree:tree_x"]
    locators = {f.locator for f in tree}
    assert "nodes.n1.text" in locators  # the 押 line
    assert "nodes.n2.text" in locators  # the 赌 line
    assert all(f.ref == "dialogue_tree:tree_x" for f in tree)  # ref stays object-level (A2)
    assert len({f.finding_id for f in tree}) == len(tree) >= 2  # a distinct id per located sentence
    n2 = next(f for f in tree if f.locator == "nodes.n2.text")
    assert "赌注" in n2.evidence  # the exact sentence is the evidence…
    assert "押" not in n2.evidence  # …not the whole tree's joined text


def test_sentence_level_multi_sentence_field_points_at_the_matching_sentence() -> None:
    bundle = ContentBundle(
        entities={
            "npc_x": Entity(
                id="npc_x",
                name="老周",
                type=EntityType.NPC,
                description="他白天在码头搬货。夜里却在后巷聚众赌博。",
            )
        }
    )
    report = ThemeSweepService(bundle=bundle).sweep("赌博")
    hits = [f for f in report.hits if f.ref == "entity:npc_x"]
    assert len(hits) == 1
    finding = hits[0]
    assert finding.locator == "description（第2句）"  # the gambling sentence, not the object
    assert "后巷聚众赌博" in finding.evidence
    assert "搬货" not in finding.evidence  # the clean first sentence is not dragged in


# ------------------------------------------------------------------- dismiss / persistence (A1+A5)
def test_dismiss_one_finding_persists_across_reruns(tmp_path) -> None:
    bundle = _sweep_bundle()
    report1 = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    target = next(f for f in report1.hits if f.ref == "entity:npc_dicer")
    assert target.finding_id  # every finding carries a stable dismissal key

    store = SweepExclusionStore(tmp_path / "world")
    store.dismiss(target, "赌", operator="ed", reason="市井背景，非赌博内容")

    # A fresh service AND a fresh store instance (a later session) must still exclude it.
    later_store = SweepExclusionStore(tmp_path / "world")
    report2 = ThemeSweepService(bundle=bundle).sweep(
        "赌", extra_terms=["押"], exclusions=later_store.dismissed_ids("赌")
    )
    assert target.finding_id not in {f.finding_id for f in report2.findings}
    assert "entity:npc_dicer" not in {f.ref for f in report2.findings}  # its only match is gone
    assert report2.dismissed_count == 1  # honestly counted
    assert any(f.ref == "quest:quest_debt" for f in report2.hits)  # everything else survives


def test_dismissal_is_object_grain_and_survives_layer_change(tmp_path) -> None:
    # A5 key stability: dismissing a match while it is a SEMANTIC review must also suppress it when
    # a later run (LLM judge on) would re-flag the SAME object as a HIT. The dismissal key is
    # object-grain and layer-independent, so toggling the judge does not defeat the exclusion.
    bundle = ContentBundle(
        entities={
            "npc_owner": Entity(
                id="npc_owner",
                name="金三爷",
                type=EntityType.NPC,
                description="码头一带的地下钱庄主人。",
            )
        }
    )
    theme = "钱财博彩"  # no literal match; stub semantic + offline judge both relate npc_owner

    r1 = ThemeSweepService(bundle=bundle, embedder=_StubSemanticEmbedder()).sweep(
        theme, semantic_threshold=0.5
    )
    review = next(f for f in r1.review_suggested if f.ref == "entity:npc_owner")
    assert review.layer == "semantic"
    assert review.finding_id == _finding_id("entity:npc_owner", _OBJECT_KEY)  # object-grain id

    store = SweepExclusionStore(tmp_path / "world")
    store.dismiss(review, theme, operator="ed")

    r2 = ThemeSweepService(
        bundle=bundle, gateway=_offline_judge_gateway(), embedder=_StubSemanticEmbedder()
    ).sweep(theme, use_llm=True, exclusions=store.dismissed_ids(theme))
    assert "entity:npc_owner" not in {f.ref for f in r2.findings}  # still gone under a new layer
    assert r2.dismissed_count == 1


def test_finding_id_is_stable_across_independent_runs() -> None:
    bundle = _sweep_bundle()
    a = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    b = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    assert {f.finding_id for f in a.findings} == {f.finding_id for f in b.findings}
    # extra synonyms that do not change WHICH sentence matches must not change a finding's id
    c = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押", "钱庄"])
    dicer_a = next(f for f in a.hits if f.ref == "entity:npc_dicer")
    dicer_c = next(f for f in c.hits if f.ref == "entity:npc_dicer")
    assert dicer_a.finding_id == dicer_c.finding_id


def test_exclusion_scoped_by_theme_and_restorable(tmp_path) -> None:
    bundle = _sweep_bundle()
    report = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    target = next(f for f in report.hits if f.ref == "quest:quest_debt")

    store = SweepExclusionStore(tmp_path / "world")
    store.dismiss(target, "赌", operator="ed")
    assert target.finding_id in store.dismissed_ids("赌")
    assert target.finding_id not in store.dismissed_ids("暴力")  # a different theme is unaffected

    assert store.restore("赌", target.finding_id) is True
    assert target.finding_id not in store.dismissed_ids("赌")
    assert store.restore("赌", target.finding_id) is False  # idempotent


def test_dismiss_requires_a_signature(tmp_path) -> None:
    bundle = _sweep_bundle()
    report = ThemeSweepService(bundle=bundle).sweep("赌")
    target = report.hits[0]
    store = SweepExclusionStore(tmp_path / "world")
    with pytest.raises(ValueError, match="署名"):
        store.dismiss(target, "赌", operator="  ")


def test_work_order_carries_dismissal_handles(tmp_path) -> None:
    bundle = _sweep_bundle()
    store = SweepExclusionStore(tmp_path / "world")
    report1 = ThemeSweepService(bundle=bundle).sweep("赌", extra_terms=["押"])
    target = next(f for f in report1.hits if f.ref == "entity:npc_dicer")
    store.dismiss(target, "赌", operator="ed")

    report2 = ThemeSweepService(bundle=bundle).sweep(
        "赌", extra_terms=["押"], exclusions=store.dismissed_ids("赌")
    )
    md = render_sweep_markdown(report2)
    assert "排除码" in md  # each line exposes the handle to 逐一排除 that exact match
    assert "已排除（逐一排除" in md and "1 条" in md  # honest suppression count in the header
