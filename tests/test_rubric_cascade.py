"""StyleRubric (12th canon type) + the sweep rubric cascade (Station-5 style/element check).

Two deliverables exercised here, both $0/offline (deterministic term/concept layers + the offline
judge double):

  * StyleRubric as a first-class canon content type — file-backed store round-trip, stable
    content_hash, byte-compat for every pre-existing (rubric-free) world, copy-on-write version
    overlay (must not break D4's version tree), and export via the generic content bundle.
  * The L1 词表/正则 → L2 概念 → L3 判官 cascade recall: an example checklist recalls its elements
    across the whole world, every finding carries evidence + the frozen 清单指纹, and each level's
    contribution is separately measurable so recall@L1 / recall@L1∪L2 can be attributed per level.
"""

from __future__ import annotations

import json

from owcopilot.assist.sweep import (
    OfflineSweepJudgeProvider,
    RubricSweepService,
    render_rubric_markdown,
)
from owcopilot.content.hash import content_hash
from owcopilot.content.models import (
    POI,
    ContentBundle,
    Entity,
    EntityType,
    StyleElementKind,
    StyleElementPolarity,
    StyleRubric,
    StyleRubricElement,
)
from owcopilot.content.store import ContentStore
from owcopilot.exporters.json_bundle import export_content_bundle
from owcopilot.llm.cache import HashingEmbedder, NoOpCache
from owcopilot.llm.gateway import LLMGateway
from owcopilot.llm.router import StaticRouter
from owcopilot.llm.telemetry import TelemetryCollector


# --------------------------------------------------------------------------- fixtures / doubles
class _StubReticenceEmbedder:
    """Deterministic semantic stand-in (model_id ``st:*`` so the cascade treats it as real). Maps
    any text carrying a 含蓄/留白 marker to one axis and everything else to an orthogonal one, so a
    concept element catches paraphrased reticence with NO shared surface token with its query."""

    model_id = "st:stub"
    _MARKERS = ("含蓄", "内敛", "留白", "收敛", "克制", "欲言", "咽下")

    def embed(self, text: str) -> list[float]:
        return self.embed_many([text])[0]

    def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if any(m in t for m in self._MARKERS) else [0.0, 1.0] for t in texts]


def _offline_judge_gateway() -> LLMGateway:
    return LLMGateway(
        providers={"cheap": OfflineSweepJudgeProvider()},
        router=StaticRouter(mapping={"theme_sweep": "cheap"}),
        cache=NoOpCache(),
        telemetry=TelemetryCollector(),
    )


def _wafu_rubric() -> StyleRubric:
    """A small "和风浓度" checklist: two lexical presence elements, a lexical taboo, one concept."""
    return StyleRubric(
        id="wafu",
        label="和风浓度 v1",
        abstract_tag="日式风格",
        version="1",
        author="ed",
        elements=[
            StyleRubricElement(
                id="je_weapon_katana",
                text="出现日本刀剑器物（武士刀/太刀/胁差/打刀）",
                kind=StyleElementKind.LEXICAL,
                terms=["武士刀", "太刀", "胁差", "打刀"],
                polarity=StyleElementPolarity.INCLUDE,
            ),
            StyleRubricElement(
                id="je_space_tatami",
                text="居住/仪式空间含和室要素（榻榻米/障子/缘侧/和室）",
                kind=StyleElementKind.LEXICAL,
                terms=["榻榻米", "障子", "缘侧", "和室"],
                polarity=StyleElementPolarity.INCLUDE,
            ),
            StyleRubricElement(
                id="je_no_cross",
                text="不应出现十字架/教堂等西方宗教符号",
                kind=StyleElementKind.LEXICAL,
                terms=["十字架", "教堂"],
                polarity=StyleElementPolarity.EXCLUDE,
            ),
            StyleRubricElement(
                id="je_tone_reticence",
                text="情绪含蓄内敛，冲突通过留白而非直述表达",
                kind=StyleElementKind.SEMANTIC,
                terms=[],  # cannot be spelled as a word list — only L2/L3 can reach it
                exemplars_pos=["欲言又止，终究把话咽下"],
            ),
        ],
    )


def _wafu_bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_samurai": Entity(
                id="npc_samurai",
                name="佐藤剑客",
                type=EntityType.NPC,
                description="腰间挂着一把武士刀，太刀出鞘寒光凛冽。",
            ),
            "npc_priest_west": Entity(
                id="npc_priest_west",
                name="西境神父",
                type=EntityType.NPC,
                description="教堂钟声响起，十字架高悬于祭坛。",  # EXCLUDE violation
            ),
            "npc_reticent": Entity(
                id="npc_reticent",
                name="沉默浪人",
                type=EntityType.NPC,
                # paraphrased reticence: shares NO term with the concept element's query
                description="他把锋芒都收敛起来，克制着不言语。",
            ),
            "npc_blacksmith": Entity(
                id="npc_blacksmith",
                name="西幻铁匠",
                type=EntityType.NPC,
                description="西式熔炉叮当作响，全然无关。",  # negative control
            ),
        },
        pois={
            "poi_dojo": POI(
                id="poi_dojo",
                name="道场",
                purpose="和室铺着榻榻米，障子透进微光。",
            )
        },
    )


# ============================================================ StyleRubric canon type (A1 / A2)
def test_style_rubric_round_trips_through_the_store(tmp_path) -> None:
    store = ContentStore(tmp_path / "world")
    bundle = _wafu_bundle()
    bundle.style_rubrics["wafu"] = _wafu_rubric()
    store.save(bundle)

    loaded = ContentStore(tmp_path / "world").load()
    assert set(loaded.style_rubrics) == {"wafu"}
    reloaded = loaded.style_rubrics["wafu"]
    assert reloaded == bundle.style_rubrics["wafu"]  # full-fidelity round-trip
    assert reloaded.version == "1"
    assert len(reloaded.elements) == 4
    assert reloaded.elements[2].polarity == StyleElementPolarity.EXCLUDE
    # aggregate JSON, one file, keyed by id (mirrors style_guides.json)
    assert (tmp_path / "world" / "world" / "style_rubrics.json").exists()


def test_rubric_hash_is_stable_and_export_carries_it(tmp_path) -> None:
    rubric = _wafu_rubric()
    # frozen fingerprint is deterministic over content — two identical rubrics hash the same
    assert content_hash(rubric) == content_hash(_wafu_rubric())
    # changing one element moves the fingerprint (a stale report can't masquerade as current)
    mutated = _wafu_rubric()
    mutated.elements[0].terms.append("刀鞘")
    assert content_hash(mutated) != content_hash(rubric)

    bundle = _wafu_bundle()
    bundle.style_rubrics["wafu"] = rubric
    manifest = export_content_bundle(bundle, tmp_path / "out")
    payload = json.loads((tmp_path / "out" / "content_bundle.json").read_text(encoding="utf-8"))
    assert "wafu" in payload["style_rubrics"]  # the rubric travels in the generic export
    assert payload["style_rubrics"]["wafu"]["elements"][0]["id"] == "je_weapon_katana"
    assert manifest.content_hash == content_hash(bundle)


def test_empty_rubric_collection_keeps_every_existing_world_byte_identical(tmp_path) -> None:
    # The 12th collection must be INVISIBLE to worlds that have no rubric: an empty style_rubrics is
    # dropped from the dump, so content_hash / export / on-disk tree are byte-for-byte as before.
    bundle = _wafu_bundle()
    assert "style_rubrics" not in bundle.model_dump(mode="json", exclude_none=True)
    # a rubric-free world writes no style_rubrics.json (on-disk tree unchanged)
    store = ContentStore(tmp_path / "world")
    store.save(bundle)
    assert not (tmp_path / "world" / "world" / "style_rubrics.json").exists()
    # its content_hash equals a bundle that never knew about the field
    assert content_hash(bundle) == content_hash(ContentBundle.model_validate(bundle.model_dump()))


# ------------------------------------------------- D4 version tree not broken by the new type (A2)
def test_rubric_participates_in_the_copy_on_write_version_overlay(tmp_path) -> None:
    store = ContentStore(tmp_path / "world")
    base = _wafu_bundle()
    base.style_rubrics["wafu"] = _wafu_rubric()
    store.save(base)  # baseline v1

    store.create_version("work__x", base_version="v1")
    derived = store.load_scoped(version="work__x")
    assert derived.style_rubrics["wafu"] == base.style_rubrics["wafu"]  # inherited via COW

    # override the rubric on the derived version only (replace with a freshly built v2 object, the
    # realistic edit pattern — a new model instance rather than an in-place field poke on the loaded
    # one, which every dict-collection loader shares by reference).
    bumped = _wafu_rubric()
    bumped.version = "2"
    bumped.elements[0].terms.append("刀鞘")
    derived.style_rubrics["wafu"] = bumped
    store.save_scoped(derived, version="work__x")

    assert store.load_scoped(version="v1").style_rubrics["wafu"].version == "1"  # base untouched
    assert store.load_scoped(version="work__x").style_rubrics["wafu"].version == "2"
    # the diff wrote only the overridden object into the version dir (copy-on-write)
    override = json.loads(
        (tmp_path / "world" / "versions" / "work__x" / "world" / "style_rubrics.json").read_text(
            encoding="utf-8"
        )
    )
    assert override["wafu"]["version"] == "2"


def test_rubric_tombstone_removes_it_on_a_derived_version(tmp_path) -> None:
    store = ContentStore(tmp_path / "world")
    base = _wafu_bundle()
    base.style_rubrics["wafu"] = _wafu_rubric()
    store.save(base)
    store.create_version("work__y", base_version="v1")

    dropped = store.load_scoped(version="work__y")
    del dropped.style_rubrics["wafu"]
    store.save_scoped(dropped, version="work__y")

    assert "wafu" in store.load_scoped(version="v1").style_rubrics  # base keeps it
    assert "wafu" not in store.load_scoped(version="work__y").style_rubrics  # whiteout on derived


# ================================================================ cascade recall (A1)
def test_cascade_recalls_lexical_elements_with_evidence_and_fingerprint() -> None:
    rubric = _wafu_rubric()
    report = RubricSweepService(bundle=_wafu_bundle()).sweep(rubric)

    # the report is pinned to the exact checklist version by its frozen fingerprint
    assert report.rubric_fingerprint == content_hash(rubric)
    assert report.rubric_version == "1"

    by_id = {e.element_id: e for e in report.elements}
    # L1 presence elements
    assert "entity:npc_samurai" in by_id["je_weapon_katana"].l1_refs
    assert "poi:poi_dojo" in by_id["je_space_tatami"].l1_refs
    # L1 taboo element: a hit is a VIOLATION and the evidence says so
    cross = by_id["je_no_cross"]
    assert "entity:npc_priest_west" in cross.l1_refs
    finding = next(f for f in cross.findings if f.ref == "entity:npc_priest_west")
    assert "禁忌命中" in finding.evidence and "十字架" in finding.evidence
    assert finding.locator  # located to the field/sentence, not just the object
    # the negative control is caught by nothing
    assert all("entity:npc_blacksmith" not in e.l1_refs for e in report.elements)


def test_per_level_miss_rate_is_measurable_from_the_result() -> None:
    # A labelled ground-truth set lets a test compute recall@L1 and recall@L1∪L2 PER LEVEL — the
    # property that makes a cascade auditable (design §6.3). The lexical element resolves fully at
    # L1; the concept element is INVISIBLE to L1 and only enters the candidate pool at L2.
    ground_truth = {
        "je_weapon_katana": {"entity:npc_samurai"},
        "je_tone_reticence": {"entity:npc_reticent"},
    }
    report = RubricSweepService(
        bundle=_wafu_bundle(), embedder=_StubReticenceEmbedder()
    ).sweep(_wafu_rubric(), semantic_threshold=0.5)
    by_id = {e.element_id: e for e in report.elements}

    def recall(refs: set[str], gt: set[str]) -> float:
        return len(refs & gt) / len(gt)

    katana = by_id["je_weapon_katana"]
    assert recall(set(katana.l1_refs), ground_truth["je_weapon_katana"]) == 1.0

    reticence = by_id["je_tone_reticence"]
    assert recall(set(reticence.l1_refs), ground_truth["je_tone_reticence"]) == 0.0  # L1 misses it
    assert (
        recall(set(reticence.candidate_refs), ground_truth["je_tone_reticence"]) == 1.0
    )  # L1∪L2 recovers it — attributable to L2
    assert report.semantic_used is True


def test_l3_judge_upgrades_a_concept_review_to_a_confirmed_hit() -> None:
    report = RubricSweepService(
        bundle=_wafu_bundle(),
        gateway=_offline_judge_gateway(),
        embedder=_StubReticenceEmbedder(),
    ).sweep(_wafu_rubric(), use_llm=True, semantic_threshold=0.5)

    reticence = next(e for e in report.elements if e.element_id == "je_tone_reticence")
    assert "entity:npc_reticent" in reticence.l3_refs  # judge confirmed the concept candidate
    confirmed = next(f for f in reticence.findings if f.ref == "entity:npc_reticent")
    assert confirmed.verdict == "hit" and confirmed.layer == "judge"
    # the object is listed once (the superseded L2 review is dropped, not duplicated)
    assert sum(1 for f in reticence.findings if f.ref == "entity:npc_reticent") == 1
    assert report.llm_used is True and report.judged_count >= 1


def test_l2_reuses_an_injected_persistent_similarity_provider() -> None:
    # The L2 seam: a caller can route concept scoring through VectorRetriever.similarities (the
    # persisted content_vectors cache) instead of re-embedding. Here a fake provider stands in for
    # it and proves L2 runs with NO in-process embedder at all.
    calls: list[tuple[str, int]] = []

    def fake_similarity(query: str, refs: list[str]) -> dict[str, float]:
        calls.append((query, len(refs)))
        return {"entity:npc_reticent": 0.9} if "留白" in query or "咽下" in query else {}

    report = RubricSweepService(
        bundle=_wafu_bundle(), similarity_fn=fake_similarity
    ).sweep(_wafu_rubric(), semantic_threshold=0.5)

    reticence = next(e for e in report.elements if e.element_id == "je_tone_reticence")
    assert reticence.l2_refs == ["entity:npc_reticent"]
    assert calls  # the persistent provider actually drove L2
    assert report.semantic_used is True


def test_semantic_layer_self_disables_on_the_hashing_stub() -> None:
    # A loud, honest disable (not a silent degrade): the hashing stub is not a real semantic model,
    # so L2 stays off and the report says so rather than surfacing char-overlap noise as "meaning".
    report = RubricSweepService(
        bundle=_wafu_bundle(), embedder=HashingEmbedder()
    ).sweep(_wafu_rubric(), semantic_threshold=0.5)
    assert report.semantic_used is False
    reticence = next(e for e in report.elements if e.element_id == "je_tone_reticence")
    assert reticence.l2_refs == []  # concept element cannot be reached without a real model
    md = render_rubric_markdown(report)
    assert "未启用" in md  # the work order admits L2 did not run


def test_two_elements_hitting_the_same_object_get_distinct_dismissal_ids() -> None:
    # The compliance case_id collision the design flags (§1.7): findings must key on the ELEMENT, so
    # two elements matching the same object dismiss independently instead of colliding.
    bundle = ContentBundle(
        entities={
            "npc_x": Entity(
                id="npc_x",
                name="双关",
                type=EntityType.NPC,
                description="他持武士刀，脚下是榻榻米。",  # hits two different lexical elements
            )
        }
    )
    report = RubricSweepService(bundle=bundle).sweep(_wafu_rubric())
    ids = {
        e.element_id: [f.finding_id for f in e.findings if f.ref == "entity:npc_x"]
        for e in report.elements
        if e.findings
    }
    katana_ids = ids["je_weapon_katana"]
    tatami_ids = ids["je_space_tatami"]
    assert katana_ids and tatami_ids
    assert set(katana_ids).isdisjoint(tatami_ids)  # element-aware ids never collide


def test_dismissed_exclusions_are_suppressed_and_counted() -> None:
    report1 = RubricSweepService(bundle=_wafu_bundle()).sweep(_wafu_rubric())
    katana = next(e for e in report1.elements if e.element_id == "je_weapon_katana")
    target = next(f for f in katana.findings if f.ref == "entity:npc_samurai")

    report2 = RubricSweepService(bundle=_wafu_bundle()).sweep(
        _wafu_rubric(), exclusions={target.finding_id}
    )
    katana2 = next(e for e in report2.elements if e.element_id == "je_weapon_katana")
    assert target.finding_id not in {f.finding_id for f in katana2.findings}
    assert report2.dismissed_count == 1


def test_work_order_states_the_coverage_ceiling_first() -> None:
    # Product 口径, non-negotiable: the FIRST thing the work order says is that coverage is bounded
    # by the checklist, and elements outside it are structurally invisible.
    report = RubricSweepService(bundle=_wafu_bundle()).sweep(_wafu_rubric())
    md = render_rubric_markdown(report)
    head = md.split("\n\n", 3)
    assert "覆盖上限 = 清单完备性" in md
    assert "清单外的未定义元素永远扫不到" in md
    # the ceiling line precedes the findings
    assert md.index("覆盖上限") < md.index("## ")
    # the frozen fingerprint and per-level counts are on the order
    assert report.rubric_fingerprint[:12] in md
    assert "L1" in md and "L2" in md and "L3" in md
    assert head  # sanity: header block parsed
