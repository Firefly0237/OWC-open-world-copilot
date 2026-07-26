# 检索评测集：n≥100 冻结版本化 qrels + recall@k + Wilson CI（BEIR/MTEB/ARES 惯例）。
"""A frozen, versioned retrieval evaluation set (n>=100) with recall@k and Wilson CIs.

This closes the self-acknowledged gap in ``acceptance.retrieval_eval_queries`` (its own docstring
says "15 queries is too small ... n >= 100 needed for Wilson CI"). It follows the standard IR
evaluation conventions, reusing — never re-deriving — the machinery the project already has:

* **BEIR** (arXiv 2104.08663) qrels shape ``{query -> {relevant refs}}`` scored over a top-k
  ranked list. ``recall@k`` is defined on the ranked list truncated at ``k`` and is **not** coupled
  to any downstream token/context budget. Only queries that carry a positive relevance judgement are
  counted toward recall — which is the canonical justification for excluding the ``unanswerable``
  category from the recall denominator. ``k in {1, 3, 5, 10}`` is a legitimate BEIR ``k_values``
  choice.
* **MTEB** (arXiv 2210.07316) frozen/versioned eval-set convention: the query set is regenerable
  byte-for-byte and pinned by ``version`` + a ``content_hash`` over the world it is derived from, so
  any drift in the underlying world is loudly detected rather than silently changing the benchmark.
  The by-category and by-locale decomposition is MTEB-standard per-task/per-language breakdown.
* **ARES** (arXiv 2311.09476) synthetic query generation: queries are derived deterministically and
  programmatically from the in-domain passage set (``build_acceptance_world()`` canon rows) by
  enumerating their entities/relations. The optional LLM-paraphrase augmentation
  (``source='llm_paraphrase'``) is out of this $0 default build — it is opt-in and approval-gated.
* **Wilson score interval** (Wilson 1927): the ``success@k`` hit-rate is a Bernoulli trial per query
  (MS MARCO ``success@k``), so its 95% interval is the project's existing
  ``assist.calibration._wilson_interval`` — **reused verbatim**, not re-implemented. For our
  dominant single-relevant queries ``recall@k`` and ``success@k`` coincide, so the Wilson interval
  on ``success@k`` reads identically as the recall interval.

Scope note: this module is intentionally **not** wired into the $0 acceptance gate. Under the
deterministic ``HashingEmbedder`` the absolute recall numbers are not a claim about semantic quality
(same honesty caveat as the acceptance gate's HashingEmbedder pin); quality numbers belong in a
dated live report. The $0 tests only assert structural integrity (label completeness, purity,
determinism) and the *mathematical* recall@k monotonicity — never a quality threshold.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, Field

from ..assist.calibration import _wilson_interval
from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..content.store import ContentStore
from ..llm.cache import Embedder, HashingEmbedder
from ..pipeline.project import ProjectContext
from .acceptance import (
    _REGIONS,
    build_acceptance_world,
    retrieval_benchmark_queries,
)

DATASET_VERSION = "v1"
# The frozen set is a deterministic template expansion (no randomness in the default $0 build); the
# seed pins reproducibility for any future ARES-style randomized augmentation and is recorded so the
# frozen version is fully specified. It is NOT a source of query-order nondeterminism today.
DATASET_SEED = 14
# k in {1, 3, 5, 10} — a standard BEIR k_values choice for a small, single-relevant qrels set.
DEFAULT_K_VALUES: tuple[int, ...] = (1, 3, 5, 10)

# The committed frozen artifact (MTEB convention): builder writes it, the eval reads it, and a test
# asserts the live build still matches it byte-for-model so world drift is caught loudly.
FROZEN_DATASET_PATH = (
    Path(__file__).resolve().parents[3]
    / "benchmarks"
    / f"retrieval_eval_set_{DATASET_VERSION}.json"
)


class RetrievalQueryCategory(str, Enum):
    """The four qrels classes. ``unanswerable`` carries no positive judgement (BEIR: excluded from
    the recall denominator); it exists to exercise refusal via the offline QA path."""

    VERBATIM = "verbatim"
    PARAPHRASE = "paraphrase"
    INDIRECT = "indirect"
    UNANSWERABLE = "unanswerable"


class RetrievalQuerySource(str, Enum):
    """Provenance of the query text. ``template`` = deterministic $0 expansion of the world;
    ``llm_paraphrase`` = ARES-style synthetic augmentation (opt-in, networked at generation)."""

    TEMPLATE = "template"
    LLM_PARAPHRASE = "llm_paraphrase"


class RetrievalEvalQuery(BaseModel):
    """One evaluation query. ``expected_refs`` is the relevance judgement (qrels row); empty
    exactly for the unanswerable class. Refs are production ``type:id`` strings (``entity:...``).

    ``evidence_hops`` is the per-hop gold evidence chain for a **multi-hop** (indirect) query: each
    inner list holds the citable ref(s) for one reasoning hop, terminating in the answer entity
    (whose ref also appears in ``expected_refs``). It is empty for single-hop queries. The multi-hop
    admission gate (``evaluation/ab_compare.py``) reads it to verify — hop by hop — that every gold
    evidence node actually exists in the indexed corpus before a multi-hop query is allowed into the
    scored set, separating "the question is unanswerable" from "retrieval missed it"."""

    query_id: str
    text: str
    locale: str  # "zh-CN" | "en"
    category: RetrievalQueryCategory
    expected_refs: list[str] = Field(default_factory=list)
    source: RetrievalQuerySource = RetrievalQuerySource.TEMPLATE
    evidence_hops: list[list[str]] = Field(default_factory=list)

    @property
    def is_answerable(self) -> bool:
        return bool(self.expected_refs)

    @property
    def is_multihop(self) -> bool:
        """A query that carries a declared multi-hop gold evidence chain (the indirect class)."""
        return bool(self.evidence_hops)


class RetrievalEvalDataset(BaseModel):
    """A frozen, versioned query set derived from a specific world snapshot.

    ``world_content_hash`` pins the world the queries were generated from (MTEB freeze anchor); if
    the world changes, the hash changes and the frozen-set drift test fails loudly."""

    version: str
    seed: int
    world_content_hash: str
    queries: list[RetrievalEvalQuery]

    @property
    def answerable(self) -> list[RetrievalEvalQuery]:
        return [q for q in self.queries if q.is_answerable]

    @property
    def unanswerable(self) -> list[RetrievalEvalQuery]:
        return [q for q in self.queries if not q.is_answerable]

    def content_digest(self) -> str:
        """A stable identifier for this frozen set (version + seed + world + queries)."""
        return content_hash(self)


class CategoryRecall(BaseModel):
    """recall@k and success@k for one slice (overall / a category / a locale), with a per-k Wilson
    95% CI over the Bernoulli success@k count. ``n`` is the number of scored (answerable) queries in
    the slice — the recall denominator."""

    label: str
    n: int
    recall_at_k: dict[int, float] = Field(default_factory=dict)
    success_at_k: dict[int, int] = Field(default_factory=dict)
    wilson_ci_at_k: dict[int, list[float] | None] = Field(default_factory=dict)


class RetrievalRecallReport(BaseModel):
    """The full recall@k report: overall plus by-category and by-locale decomposition. Carries the
    dataset version/hash so a number is always traceable to the exact frozen set behind it."""

    version: str
    dataset_hash: str
    k_values: list[int]
    scored_queries: int  # answerable queries — the recall denominator (BEIR)
    excluded_unanswerable: int  # counted, then excluded from recall (BEIR)
    overall: CategoryRecall
    by_category: dict[str, CategoryRecall] = Field(default_factory=dict)
    by_locale: dict[str, CategoryRecall] = Field(default_factory=dict)


# --------------------------------------------------------------------------------------------------
# Deterministic template expansion of build_acceptance_world().
# --------------------------------------------------------------------------------------------------

_LOCALE_ZH = "zh-CN"
_LOCALE_EN = "en"

# Paraphrase queries: describe the target by attribute / function / relationship WITHOUT ever
# spelling its canonical name or any alias (the purity test enforces this). One zh + one en each.
# For the 15 distinctive named entities the descriptions are hand-authored; the 10 region beacons
# are templated from the region name (fair game — the region name is not the location's own name).
_PARAPHRASE_ENTITIES: list[tuple[str, str, str]] = [
    (
        "fac_iron",
        "镇守北境群山隘口的最大常备军团",
        "the standing legion that guards the northern mountain passes",
    ),
    (
        "fac_trade",
        "统筹边地商队与货运路线的行会组织",
        "the guild coordinating caravans and freight routes on the frontier",
    ),
    (
        "fac_mist",
        "与戈壁劫掠帮派为敌的隐秘盟会",
        "the secretive pact at war with the desert raiders",
    ),
    (
        "fac_sand",
        "盘踞荒漠、以劫掠商旅为生的帮派",
        "the marauder band living off raiding travelers in the wastes",
    ),
    (
        "evt_mist_fire",
        "多年前焚毁隐秘盟据点的那场大火灾",
        "the great blaze that razed the hidden pact's stronghold years ago",
    ),
    (
        "evt_xuanwu_pact",
        "北方诸方缔结的那纸镇北盟誓",
        "the northern oath sworn to bind the frontier powers together",
    ),
    (
        "evt_salt_battle",
        "在海岸线上爆发的那场著名海战",
        "the famous naval clash fought along the coastline",
    ),
    (
        "evt_road_fall",
        "使古老商道彻底陷落的那次浩劫",
        "the catastrophe that brought the ancient trade road to ruin",
    ),
    (
        "evt_ironwall_siege",
        "边关要塞遭长期围困的那场战事",
        "the prolonged siege laid against the frontier fortress gateway",
    ),
    (
        "evt_canglang_flood",
        "河堤决口酿成的那场吞没谷地的大水",
        "the great flood loosed when the river levee broke and drowned the valley",
    ),
    (
        "item_xuantie_seal",
        "由北军签发、准许通行关隘的凭证",
        "the token issued by the northern army granting safe passage through the checkpoints",
    ),
    (
        "item_mist_map",
        "标注隐秘小径与暗哨的行军地图",
        "the campaign chart marking hidden trails and covert lookout posts",
    ),
    (
        "item_salt_permit",
        "贩运食盐所必需的官方许可文书",
        "the official license required to haul and sell salt",
    ),
    (
        "concept_caoyun",
        "古代经由水路调运粮货的管理体制",
        "the ancient administration for moving grain and goods by waterway",
    ),
    (
        "concept_beacon",
        "以点燃烟火逐台传警的边防制度",
        "the frontier signaling system that relays alarms from tower to tower by lit smoke",
    ),
]

# Indirect queries: describe the answer entity ONLY via a graph hop (never by name).
# quest -> giver_npc -> member_of -> faction. In build_acceptance_world an odd quest_index gives the
# "a" giver (member_of fac_iron); an even quest_index gives the "b" giver (member_of fac_trade).
_INDIRECT_QUEST_GIVER: list[tuple[int, int, str]] = [
    (1, 1, "fac_iron"),
    (2, 2, "fac_trade"),
    (3, 1, "fac_iron"),
    (4, 2, "fac_trade"),
    (5, 1, "fac_iron"),
    (6, 2, "fac_trade"),
]
# location -> controlled_by -> faction. For the "a" (beacon) location the controller is
# npc_factions[region_index % 3] with npc_factions = [fac_iron, fac_trade, fac_mist].
_INDIRECT_LOC_CONTROL: list[tuple[int, str]] = [
    (1, "fac_trade"),
    (2, "fac_mist"),
    (3, "fac_iron"),
    (4, "fac_trade"),
    (5, "fac_mist"),
    (6, "fac_iron"),
]

_CN_ORDINALS = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

# Unanswerable queries: genuinely out-of-world topics (dragons, the moon, aliens, chocolate,
# unicorns, beanstalks, ...). Their token set (CJK chars/bigrams and latin words) is disjoint from
# the frontier world, so the production retrieval path returns an empty pack and the reused offline
# QA path refuses — the reusable "teeth" for the negative class (no invented threshold). Every one
# of these was verified to refuse through the real LoreQAService+OfflineQAProvider path; keep them
# out-of-world if edited (see test_retrieval_eval_dataset.py::test_unanswerable_are_refused).
_UNANSWERABLE_ZH: list[str] = [
    "龙王住在哪座海底宫殿",
    "谁偷走了夜空里的月亮",
    "独角兽最爱吃什么野花",
    "巧克力喷泉停在哪朵云上",
    "外星飞碟何时降落",
    "彩虹尽头埋着多少黄金",
    "精灵公主住在哪棵大树",
    "会说话的南瓜马车去哪了",
]
_UNANSWERABLE_EN: list[str] = [
    "who is the dragon king",
    "how do unicorns fly",
    "when will the spaceship land",
    "which castle hides the fairy queen",
    "how tall is the giant beanstalk",
    "which mermaid guards the coral palace",
    "how many wishes does the lamp grant",
    "which planet grows candy trees",
]


def _looks_chinese(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _verbatim_queries() -> list[RetrievalEvalQuery]:
    """Reuse ``acceptance.retrieval_benchmark_queries`` (production code) as the verbatim control
    arm; locale is inferred from the text (CJK -> zh-CN, else en)."""
    out: list[RetrievalEvalQuery] = []
    for index, (text, ref) in enumerate(retrieval_benchmark_queries()):
        locale = _LOCALE_ZH if _looks_chinese(text) else _LOCALE_EN
        out.append(
            RetrievalEvalQuery(
                query_id=f"vb-{index:03d}",
                text=text,
                locale=locale,
                category=RetrievalQueryCategory.VERBATIM,
                expected_refs=[ref],
            )
        )
    return out


def _paraphrase_queries() -> list[RetrievalEvalQuery]:
    out: list[RetrievalEvalQuery] = []
    counter = 0
    for entity_id, zh_text, en_text in _PARAPHRASE_ENTITIES:
        ref = f"entity:{entity_id}"
        for locale, text in ((_LOCALE_ZH, zh_text), (_LOCALE_EN, en_text)):
            out.append(
                RetrievalEvalQuery(
                    query_id=f"pp-{counter:03d}",
                    text=text,
                    locale=locale,
                    category=RetrievalQueryCategory.PARAPHRASE,
                    expected_refs=[ref],
                )
            )
            counter += 1
    for region_index, (region_cn, region_en, _lo, _hi) in enumerate(_REGIONS, start=1):
        ref = f"entity:loc_r{region_index}_a"
        zh_text = f"坐落在{region_cn}、由驻军与商队共同据守的烽火哨台"
        en_text = f"the beacon watch-post in {region_en} held jointly by the garrison and caravans"
        for locale, text in ((_LOCALE_ZH, zh_text), (_LOCALE_EN, en_text)):
            out.append(
                RetrievalEvalQuery(
                    query_id=f"pp-{counter:03d}",
                    text=text,
                    locale=locale,
                    category=RetrievalQueryCategory.PARAPHRASE,
                    expected_refs=[ref],
                )
            )
            counter += 1
    return out


def _relation_target(bundle: ContentBundle, source_id: str, kind: str) -> str:
    """The target entity id of the first ``kind`` relation out of ``source_id``.

    A grounded lookup against the real world graph (not a re-derivation of the generation formula):
    the multi-hop gold chain is read from the same relations the audit/retrieval graph indexes, so a
    drift in ``build_acceptance_world`` is caught loudly here rather than yielding a phantom hop."""
    for relation in bundle.relations:
        if relation.source == source_id and relation.kind == kind:
            return relation.target
    raise KeyError(f"no {kind!r} relation out of {source_id!r} in the acceptance world")


def _indirect_queries() -> list[RetrievalEvalQuery]:
    # The gold evidence chain for each indirect query is read from the real world graph so the
    # admission gate checks facts that live in the corpus, never a recomputed answer.
    bundle = build_acceptance_world()
    out: list[RetrievalEvalQuery] = []
    counter = 0
    for region_index, quest_index, faction_id in _INDIRECT_QUEST_GIVER:
        ref = f"entity:{faction_id}"
        quest_id = f"quest_r{region_index}_q{quest_index}"
        # hop 1: the quest names its giver; hop 2: the giver's member_of edge names the faction.
        giver_npc_id = bundle.quests[quest_id].giver_npc
        assert giver_npc_id is not None, quest_id  # every acceptance-world quest has a giver
        member_faction = _relation_target(bundle, giver_npc_id, "member_of")
        assert member_faction == faction_id, (quest_id, giver_npc_id, member_faction, faction_id)
        hops = [[f"quest:{quest_id}"], [f"entity:{giver_npc_id}"], [ref]]
        cn = _CN_ORDINALS[region_index - 1]
        zh_text = f"第{cn}行省第{_CN_ORDINALS[quest_index - 1]}个委托任务的发布者效力于哪一方势力"
        en_text = (
            f"which faction does the giver of quest {quest_index} in province {region_index} serve"
        )
        for locale, text in ((_LOCALE_ZH, zh_text), (_LOCALE_EN, en_text)):
            out.append(
                RetrievalEvalQuery(
                    query_id=f"id-{counter:03d}",
                    text=text,
                    locale=locale,
                    category=RetrievalQueryCategory.INDIRECT,
                    expected_refs=[ref],
                    evidence_hops=hops,
                )
            )
            counter += 1
    for region_index, faction_id in _INDIRECT_LOC_CONTROL:
        ref = f"entity:{faction_id}"
        loc_id = f"loc_r{region_index}_a"
        # single-bridge hop: the beacon location's controlled_by edge names the faction.
        control_faction = _relation_target(bundle, loc_id, "controlled_by")
        assert control_faction == faction_id, (loc_id, control_faction, faction_id)
        hops = [[f"entity:{loc_id}"], [ref]]
        cn = _CN_ORDINALS[region_index - 1]
        zh_text = f"第{cn}行省的烽燧据点归哪支武装力量节制"
        en_text = f"which armed force controls the beacon outpost in province {region_index}"
        for locale, text in ((_LOCALE_ZH, zh_text), (_LOCALE_EN, en_text)):
            out.append(
                RetrievalEvalQuery(
                    query_id=f"id-{counter:03d}",
                    text=text,
                    locale=locale,
                    category=RetrievalQueryCategory.INDIRECT,
                    expected_refs=[ref],
                    evidence_hops=hops,
                )
            )
            counter += 1
    return out


def _unanswerable_queries() -> list[RetrievalEvalQuery]:
    out: list[RetrievalEvalQuery] = []
    counter = 0
    for locale, texts in ((_LOCALE_ZH, _UNANSWERABLE_ZH), (_LOCALE_EN, _UNANSWERABLE_EN)):
        for text in texts:
            out.append(
                RetrievalEvalQuery(
                    query_id=f"un-{counter:03d}",
                    text=text,
                    locale=locale,
                    category=RetrievalQueryCategory.UNANSWERABLE,
                    expected_refs=[],
                )
            )
            counter += 1
    return out


def build_retrieval_eval_dataset() -> RetrievalEvalDataset:
    """Deterministically expand ``build_acceptance_world()`` into the frozen evaluation set.

    Composition (all floors from the design's acceptance criteria are met with headroom):
    verbatim 30 (control), paraphrase 50, indirect 24, unanswerable 16 — 104 answerable + 16
    unanswerable, split 50/50 zh/en. Two calls return byte-identical results."""
    queries: list[RetrievalEvalQuery] = [
        *_verbatim_queries(),
        *_paraphrase_queries(),
        *_indirect_queries(),
        *_unanswerable_queries(),
    ]
    world_hash = content_hash(build_acceptance_world())
    return RetrievalEvalDataset(
        version=DATASET_VERSION,
        seed=DATASET_SEED,
        world_content_hash=world_hash,
        queries=queries,
    )


def write_frozen_dataset(path: Path | None = None) -> Path:
    """Write the frozen JSON artifact (MTEB convention). Called by tooling, not by the $0 tests."""
    target = path or FROZEN_DATASET_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        build_retrieval_eval_dataset().model_dump_json(indent=2) + "\n", encoding="utf-8"
    )
    return target


def load_frozen_dataset(path: Path | None = None) -> RetrievalEvalDataset:
    source = path or FROZEN_DATASET_PATH
    return RetrievalEvalDataset.model_validate_json(source.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------------------------------
# recall@k evaluation (pure) + production-path runner.
# --------------------------------------------------------------------------------------------------


def _slice_recall(
    label: str,
    queries: list[RetrievalEvalQuery],
    ranked: dict[str, Sequence[str]],
    k_values: tuple[int, ...],
) -> CategoryRecall:
    n = len(queries)
    recall_at_k: dict[int, float] = {}
    success_at_k: dict[int, int] = {}
    wilson_ci_at_k: dict[int, list[float] | None] = {}
    for k in k_values:
        recall_sum = 0.0
        successes = 0
        for query in queries:
            relevant = set(query.expected_refs)
            topk = set(ranked[query.query_id][:k])
            found = relevant & topk
            # recall@k over the truncated ranked list (BEIR); success@k is the Bernoulli hit trial.
            recall_sum += len(found) / len(relevant)
            if found:
                successes += 1
        recall_at_k[k] = round(recall_sum / n, 4) if n else 0.0
        success_at_k[k] = successes
        wilson_ci_at_k[k] = _wilson_interval(successes, n)
    return CategoryRecall(
        label=label,
        n=n,
        recall_at_k=recall_at_k,
        success_at_k=success_at_k,
        wilson_ci_at_k=wilson_ci_at_k,
    )


def evaluate_retrieval_recall(
    dataset: RetrievalEvalDataset,
    ranked_refs_for: Callable[[RetrievalEvalQuery], Sequence[str]],
    *,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> RetrievalRecallReport:
    """Score recall@k over the answerable queries, decomposed overall / by category / by locale.

    ``ranked_refs_for`` returns the ranked hit refs for a query (the production retrieval path in
    :func:`run_retrieval_recall_eval`, or a synthetic oracle in tests). Unanswerable queries carry
    no positive judgement and are excluded from every recall denominator (BEIR). The metric layer is
    pure so it is testable without opening a project."""
    answerable = dataset.answerable
    ranked: dict[str, Sequence[str]] = {q.query_id: list(ranked_refs_for(q)) for q in answerable}

    overall = _slice_recall("overall", answerable, ranked, k_values)

    by_category: dict[str, CategoryRecall] = {}
    for category in RetrievalQueryCategory:
        if category is RetrievalQueryCategory.UNANSWERABLE:
            continue
        members = [q for q in answerable if q.category is category]
        if members:
            by_category[category.value] = _slice_recall(category.value, members, ranked, k_values)

    by_locale: dict[str, CategoryRecall] = {}
    for locale in sorted({q.locale for q in answerable}):
        members = [q for q in answerable if q.locale == locale]
        by_locale[locale] = _slice_recall(locale, members, ranked, k_values)

    return RetrievalRecallReport(
        version=dataset.version,
        dataset_hash=dataset.content_digest(),
        k_values=list(k_values),
        scored_queries=len(answerable),
        excluded_unanswerable=len(dataset.unanswerable),
        overall=overall,
        by_category=by_category,
        by_locale=by_locale,
    )


# A budget large enough that the token-budget trim is a no-op, so ``ContextPack.refs`` is the full
# ranked order and recall@k reads the ranking rather than the packing (the FP2 vs FP3 distinction:
# this measures rank-only recall, not post-budget pack hit-rate).
_UNTRIMMED_BUDGET = 1_000_000


def _acceptance_bundle_and_root(workspace: str | Path) -> tuple[ContentBundle, Path]:
    root = Path(workspace)
    world_root = root / "retrieval_recall_world"
    bundle = build_acceptance_world()
    ContentStore(world_root).save(bundle)
    return bundle, world_root


def run_retrieval_recall_eval(
    workspace: str | Path,
    *,
    dataset: RetrievalEvalDataset | None = None,
    embedder: Embedder | None = None,
    k_values: tuple[int, ...] = DEFAULT_K_VALUES,
) -> RetrievalRecallReport:
    """Run recall@k over the frozen set through the **production** retrieval path.

    Saves the acceptance world, opens a real :class:`ProjectContext`, and reads
    ``context_builder.build(text, budget=untrimmed).refs`` as the ranked list. Defaults to the
    deterministic :class:`HashingEmbedder` so it is $0 and reproducible (semantic quality is not the
    claim here — inject a semantic embedder for a dated live report). Not wired into the CI gate."""
    ds = dataset or build_retrieval_eval_dataset()
    root = Path(workspace)
    _bundle, world_root = _acceptance_bundle_and_root(root)
    project = ProjectContext.open(
        world_root,
        sqlite_path=root / "retrieval_recall_eval.sqlite",
        embedder=embedder or HashingEmbedder(),
    )
    try:
        limit = max(k_values)
        # Encode every scored query in a single batched forward pass (BEIR/SentenceTransformers
        # ``encode_queries``) instead of one embed per query, then look the ranked list up per query
        # — bit-identical to a per-query ``build`` under the deterministic embedder.
        answerable = ds.answerable
        packs = project.context_builder.build_many(
            [q.text for q in answerable], budget_tokens=_UNTRIMMED_BUDGET, limit=limit
        )
        ranked_by_id = {q.query_id: pack.refs for q, pack in zip(answerable, packs, strict=True)}

        def ranked_refs_for(query: RetrievalEvalQuery) -> Sequence[str]:
            return ranked_by_id[query.query_id]

        return evaluate_retrieval_recall(ds, ranked_refs_for, k_values=k_values)
    finally:
        project.close()


# --------------------------------------------------------------------------------------------------
# Staged FP2/FP3 retrieval diagnostic (Barnett et al. 2024, "Seven Failure Points When Engineering
# a RAG System", CAIN 2024, arXiv:2401.05856).
#
# The acceptance gate's ``_retrieval_hit_rate`` reports ONE post-budget-pack number, so a miss there
# cannot be attributed to ranking vs budget-trim. Barnett splits this into two failure points:
#   FP2 "Missed the Top Ranked Documents" — the answer never entered the returned candidate pool
#        (a retrieval/ranking failure);
#   FP3 "Not in Context — Consolidation strategy Limitations" — the answer WAS in the returned pool
#        but got dropped when the pool was consolidated to fit the context/token budget.
# In our pipeline (retrieval/context_pack.py:ContextPackBuilder._assemble) the fusion+rerank stages
# only reorder; ``trim_hits_to_budget`` (retrieval/budget.py) is the ONLY stage that drops hits — so
# the FP2/FP3 boundary is exactly the trim stage. FP2 = membership in the untrimmed returned pool;
# FP3 = survival of the budget consolidation. Because trim only removes, FP3 <= FP2 by construction
# (RAGAS context-recall vs context-precision, arXiv:2309.15217, reports these two axes separately).
#
# Honest scope note: at the acceptance-world scale (50–300 nodes) the 800-token trim is usually a
# no-op, so FP3 ≈ FP2 and this split adds little signal today. It is built for the large-corpus
# target (几十万条), where budget consolidation becomes a real failure mode worth attributing.
# --------------------------------------------------------------------------------------------------

# Matches the production ``ContextPackBuilder.build`` default ``budget_tokens=800`` (retrieval/
# context_pack.py). The FP3 pack leg is measured at this production consolidation budget; the FP2
# pool leg reuses ``_UNTRIMMED_BUDGET`` so the trim is a no-op and FP3 <= FP2 is provable for any
# budget. Kept in lockstep with production by ``test_default_staged_budget_matches_production``.
DEFAULT_STAGED_BUDGET: int = 800


class StagedRetrievalDiagnostic(BaseModel):
    """One slice's split of Barnett's FP2 vs FP3 over the fusion->rerank->budget-trim pipeline.

    * ``fp2_*`` — Barnett **FP2** "Missed the Top Ranked Documents": the relevant ref is a member
      of the full *untrimmed* returned candidate pool (rank-only membership; it reached
      consolidation at all). This is the BEIR recall axis over the untrimmed pool.
    * ``fp3_*`` — Barnett **FP3** "Not in Context — Consolidation strategy Limitations": the
      relevant ref *survived* the token-budget consolidation (``trim_hits_to_budget``) into the
      returned pack.
    * ``budget_trim_loss`` = ``fp2_pool_hit_rate - fp3_pack_hit_rate`` — the share of queries whose
      evidence was retrieved into the pool but consolidated out of the context window. It is
      ``>= 0`` by construction: consolidation can only drop hits, never add them (so FP3 <= FP2).

    ``n`` is the number of scored (answerable) queries in the slice — the shared denominator. The
    Wilson 95% CIs reuse ``assist.calibration._wilson_interval`` verbatim over the Bernoulli hit
    counts."""

    label: str
    n: int
    fp2_pool_hits: int
    fp3_pack_hits: int
    fp2_pool_hit_rate: float
    fp3_pack_hit_rate: float
    budget_trim_loss: float
    fp2_wilson_ci: list[float] | None
    fp3_wilson_ci: list[float] | None


class StagedRetrievalReport(BaseModel):
    """The full FP2/FP3 diagnostic: overall plus by-category and by-locale decomposition. Carries
    the dataset version/hash and the ``budget_tokens``/``pool_limit`` the split was measured at, so
    a number is always traceable to the exact frozen set and consolidation budget behind it. Mirrors
    :class:`RetrievalRecallReport`'s traceability fields."""

    version: str
    dataset_hash: str
    budget_tokens: int
    pool_limit: int
    scored_queries: int  # answerable queries — the shared FP2/FP3 denominator (BEIR)
    excluded_unanswerable: int  # counted, then excluded (BEIR: no positive judgement)
    overall: StagedRetrievalDiagnostic
    by_category: dict[str, StagedRetrievalDiagnostic] = Field(default_factory=dict)
    by_locale: dict[str, StagedRetrievalDiagnostic] = Field(default_factory=dict)


def _slice_staged(
    label: str,
    queries: list[RetrievalEvalQuery],
    pool_map: dict[str, Sequence[str]],
    pack_map: dict[str, Sequence[str]],
) -> StagedRetrievalDiagnostic:
    n = len(queries)
    fp2_hits = 0
    fp3_hits = 0
    for query in queries:
        relevant = set(query.expected_refs)
        if relevant & set(pool_map[query.query_id]):
            fp2_hits += 1
        if relevant & set(pack_map[query.query_id]):
            fp3_hits += 1
    fp2_rate = round(fp2_hits / n, 4) if n else 0.0
    fp3_rate = round(fp3_hits / n, 4) if n else 0.0
    return StagedRetrievalDiagnostic(
        label=label,
        n=n,
        fp2_pool_hits=fp2_hits,
        fp3_pack_hits=fp3_hits,
        fp2_pool_hit_rate=fp2_rate,
        fp3_pack_hit_rate=fp3_rate,
        budget_trim_loss=round(fp2_rate - fp3_rate, 4),
        fp2_wilson_ci=_wilson_interval(fp2_hits, n),
        fp3_wilson_ci=_wilson_interval(fp3_hits, n),
    )


def evaluate_staged_retrieval(
    dataset: RetrievalEvalDataset,
    pool_refs_for: Callable[[RetrievalEvalQuery], Sequence[str]],
    pack_refs_for: Callable[[RetrievalEvalQuery], Sequence[str]],
    *,
    budget_tokens: int,
    pool_limit: int,
) -> StagedRetrievalReport:
    """Split the retrieval hit-rate into Barnett's FP2 (candidate-pool membership) and FP3 (survives
    budget consolidation) legs over the answerable queries, decomposed overall / by category / by
    locale.

    ``pool_refs_for`` returns the full *untrimmed* returned pool for a query (the FP2 axis);
    ``pack_refs_for`` returns the budget-consolidated pack (the FP3 axis). In the production runner
    both come from the SAME :class:`ContextPackBuilder`, differing only in ``budget_tokens``, so the
    pack is a subset of the pool by construction. This is enforced fail-closed: consolidation can
    only remove hits, never add them, so any query whose pack carries a ref absent from its pool is
    a contract violation and raises :class:`ValueError` rather than silently reporting FP3 > FP2.

    Unanswerable queries carry no positive judgement and are excluded from every denominator (BEIR).
    The metric layer is pure so it is testable without opening a project."""
    answerable = dataset.answerable
    pool_map: dict[str, Sequence[str]] = {q.query_id: list(pool_refs_for(q)) for q in answerable}
    pack_map: dict[str, Sequence[str]] = {q.query_id: list(pack_refs_for(q)) for q in answerable}

    for query in answerable:
        added = set(pack_map[query.query_id]) - set(pool_map[query.query_id])
        if added:
            raise ValueError(
                f"consolidation added refs absent from the pool for {query.query_id!r}: "
                f"{sorted(added)} — budget trim can only drop hits, never add them (FP3 <= FP2)"
            )

    overall = _slice_staged("overall", answerable, pool_map, pack_map)

    by_category: dict[str, StagedRetrievalDiagnostic] = {}
    for category in RetrievalQueryCategory:
        if category is RetrievalQueryCategory.UNANSWERABLE:
            continue
        members = [q for q in answerable if q.category is category]
        if members:
            by_category[category.value] = _slice_staged(category.value, members, pool_map, pack_map)

    by_locale: dict[str, StagedRetrievalDiagnostic] = {}
    for locale in sorted({q.locale for q in answerable}):
        members = [q for q in answerable if q.locale == locale]
        by_locale[locale] = _slice_staged(locale, members, pool_map, pack_map)

    return StagedRetrievalReport(
        version=dataset.version,
        dataset_hash=dataset.content_digest(),
        budget_tokens=budget_tokens,
        pool_limit=pool_limit,
        scored_queries=len(answerable),
        excluded_unanswerable=len(dataset.unanswerable),
        overall=overall,
        by_category=by_category,
        by_locale=by_locale,
    )


def run_staged_retrieval_diagnostic(
    workspace: str | Path,
    *,
    dataset: RetrievalEvalDataset | None = None,
    embedder: Embedder | None = None,
    budget_tokens: int = DEFAULT_STAGED_BUDGET,
    pool_limit: int | None = None,
) -> StagedRetrievalReport:
    """Run the FP2/FP3 staged diagnostic through the **production** retrieval path.

    Saves the acceptance world, opens a real :class:`ProjectContext`, and reads two packs from the
    SAME production :class:`ContextPackBuilder` for each query: the FP2 pool at the untrimmed budget
    (so the token-budget trim is a no-op and ``refs`` is the full returned pool) and the FP3 pack at
    ``budget_tokens`` (the production consolidation budget). Since both builds share the query
    ``limit`` the fused+reranked list is identical and the pack is a strict subset of the pool, so
    the fail-closed FP3 <= FP2 invariant holds. Defaults to the deterministic
    :class:`HashingEmbedder` so it is $0 and reproducible (semantic quality is not the claim here —
    inject a semantic embedder for a dated live report). Not wired into the CI gate, exactly like
    :func:`run_retrieval_recall_eval`."""
    ds = dataset or build_retrieval_eval_dataset()
    root = Path(workspace)
    _bundle, world_root = _acceptance_bundle_and_root(root)
    project = ProjectContext.open(
        world_root,
        sqlite_path=root / "staged_retrieval_eval.sqlite",
        embedder=embedder or HashingEmbedder(),
    )
    try:
        limit = pool_limit or max(DEFAULT_K_VALUES)
        # Batch-encode the scored queries once per budget pass (pool = untrimmed, pack = production
        # consolidation budget). The two passes still run the full pipeline at their two budgets
        # (only the trim differs) exactly as before; batching only collapses the per-query embeds
        # of each pass into one embed_many. Bit-identical to per-query build under a det. embedder.
        answerable = ds.answerable
        texts = [q.text for q in answerable]
        pool_packs = project.context_builder.build_many(
            texts, budget_tokens=_UNTRIMMED_BUDGET, limit=limit
        )
        pack_packs = project.context_builder.build_many(
            texts, budget_tokens=budget_tokens, limit=limit
        )
        pool_by_id = {q.query_id: p.refs for q, p in zip(answerable, pool_packs, strict=True)}
        pack_by_id = {q.query_id: p.refs for q, p in zip(answerable, pack_packs, strict=True)}

        def pool_refs_for(query: RetrievalEvalQuery) -> Sequence[str]:
            return pool_by_id[query.query_id]

        def pack_refs_for(query: RetrievalEvalQuery) -> Sequence[str]:
            return pack_by_id[query.query_id]

        return evaluate_staged_retrieval(
            ds, pool_refs_for, pack_refs_for, budget_tokens=budget_tokens, pool_limit=limit
        )
    finally:
        project.close()
