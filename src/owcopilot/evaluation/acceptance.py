"""Acceptance-grade evaluation: a bilingual ~65-entity world, 25 seeded errors, and benchmarks.

This is the half of the project that proves the other half. It builds 雾脊行省/Mistridge
Province — 10 regions, 65 entities, 36 quest chains, dialogues with zh-CN/en localized text —
asserts the clean world audits to **zero open issues** (false-positive gate), seeds 25 classified
errors and measures rule detection, replays three known change scenarios through impact analysis
(recall gate), runs a 30-query bilingual retrieval benchmark, and spot-checks grounded-or-refuse
QA behaviour. Everything is deterministic and offline so it can sit in CI at $0.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..content.models import (
    POI,
    ContentBundle,
    DialogueRef,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    QuestEventReference,
    QuestEventRefKind,
    RegionBrief,
    Relation,
    StyleGuide,
    Term,
)
from ..content.store import ContentStore
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import NoOpCache
from ..llm.gateway import LLMGateway
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService

DETECTION_RATE_GATE = 0.85
RETRIEVAL_HIT_RATE_GATE = 0.90

_REGIONS: list[tuple[str, str, int, int]] = [
    ("雾脊山道", "Mistridge Pass", 1, 5),
    ("苇泽沼地", "Reedmarsh Fen", 4, 8),
    ("北望平原", "Northwatch Plain", 7, 11),
    ("黑沙戈壁", "Blacksand Gobi", 10, 14),
    ("沧浪河谷", "Canglang Valley", 13, 17),
    ("铁壁关隘", "Ironwall Pass", 16, 20),
    ("落霞丘陵", "Sunsetfall Hills", 19, 23),
    ("寒杉林海", "Coldfir Forest", 22, 26),
    ("盐风海岸", "Saltwind Coast", 25, 29),
    ("古道废城", "Old Road Ruins", 28, 32),
]
_SURNAMES = ["沈", "陆", "苏", "顾", "秦", "白", "韩", "叶", "赵", "程"]
_SURNAMES_EN = ["Shen", "Lu", "Su", "Gu", "Qin", "Bai", "Han", "Ye", "Zhao", "Cheng"]
_GIVEN = ["清河", "忘川", "砚秋", "长风", "照夜", "听澜", "望舒", "折柳", "承影", "惊鸿"]
_GIVEN_EN = [
    "Qinghe",
    "Wangchuan",
    "Yanqiu",
    "Changfeng",
    "Zhaoye",
    "Tinglan",
    "Wangshu",
    "Zheliu",
    "Chengying",
    "Jinghong",
]
_FACTIONS: list[tuple[str, str, str]] = [
    ("fac_iron", "铁卫军团", "Iron Ward Legion"),
    ("fac_trade", "商路公会", "Caravan Guild"),
    ("fac_mist", "雾隐盟", "Mistveil Pact"),
    ("fac_sand", "黑沙帮", "Black Sand Gang"),
]
_EVENTS: list[tuple[str, str, int]] = [
    ("evt_mist_fire", "雾隐大火", 10),
    ("evt_xuanwu_pact", "玄武之约", 20),
    ("evt_salt_battle", "盐风海战", 30),
    ("evt_road_fall", "古道陷落", 40),
    ("evt_ironwall_siege", "铁壁之围", 50),
    ("evt_canglang_flood", "沧浪决堤", 60),
]
_ITEMS: list[tuple[str, str, EntityType]] = [
    ("item_xuantie_seal", "玄铁令", EntityType.ITEM),
    ("item_mist_map", "雾隐图", EntityType.ITEM),
    ("item_salt_permit", "盐引", EntityType.ITEM),
    ("concept_caoyun", "漕运制度", EntityType.CONCEPT),
    ("concept_beacon", "烽火制度", EntityType.CONCEPT),
]
_QUEST_TOPICS = ["巡查烽燧", "护送盐车", "调查失踪", "清剿匪患"]


class SeededError(BaseModel):
    code: str
    expected_rule: str
    target_ref: str
    note: str = ""


class AcceptanceCheck(BaseModel):
    name: str
    passed: bool
    details: dict[str, Any] = Field(default_factory=dict)


class AcceptanceReport(BaseModel):
    passed: bool
    checks: list[AcceptanceCheck]
    metrics: dict[str, Any] = Field(default_factory=dict)


def build_acceptance_world() -> ContentBundle:
    bundle = ContentBundle()

    for fac_id, name_cn, name_en in _FACTIONS:
        bundle.entities[fac_id] = Entity(
            id=fac_id,
            name=name_cn,
            type=EntityType.FACTION,
            aliases=[name_en],
            description=f"{name_cn}（{name_en}），雾脊行省的重要势力之一。",
        )
    bundle.relations.append(Relation(source="fac_iron", target="fac_trade", kind="allied_with"))
    bundle.relations.append(Relation(source="fac_iron", target="fac_sand", kind="enemy_of"))
    bundle.relations.append(Relation(source="fac_mist", target="fac_sand", kind="enemy_of"))

    for event_id, name_cn, order in _EVENTS:
        bundle.entities[event_id] = Entity(
            id=event_id,
            name=name_cn,
            type=EntityType.EVENT,
            description=f"历史事件「{name_cn}」，时间线序号 {order}。",
            metadata={"timeline_order": order},
        )
    for item_id, name_cn, entity_type in _ITEMS:
        bundle.entities[item_id] = Entity(
            id=item_id,
            name=name_cn,
            type=entity_type,
            description=f"{name_cn}，与雾脊行省的商路与防务相关。",
        )

    npc_factions = ["fac_iron", "fac_trade", "fac_mist"]
    for index, (region_cn, region_en, level_min, level_max) in enumerate(_REGIONS, start=1):
        region_id = f"region_{index:02d}"
        bundle.regions[region_id] = RegionBrief(
            id=region_id,
            name=region_cn,
            level_min=level_min,
            level_max=level_max,
            themes=["frontier", "trade-road"],
            allowed_content=["patrol", "escort", "investigation"],
            banned_content=["undead", "gore"],
        )

        loc_specs = [("a", "烽燧", "Beacon"), ("b", "渡口", "Crossing")]
        for slot, suffix_cn, suffix_en in loc_specs:
            loc_id = f"loc_r{index}_{slot}"
            bundle.entities[loc_id] = Entity(
                id=loc_id,
                name=f"{region_cn}{suffix_cn}",
                type=EntityType.LOCATION,
                aliases=[f"{region_en} {suffix_en}"],
                description=f"位于{region_cn}的{suffix_cn}，由驻军与商队共同使用。",
            )
            controller = npc_factions[(index + (0 if slot == "a" else 1)) % 3]
            bundle.relations.append(
                Relation(source=loc_id, target=controller, kind="controlled_by")
            )

        for slot_index, slot in enumerate(("a", "b", "c")):
            npc_id = f"npc_r{index}_{slot}"
            name_index = (index - 1 + slot_index * 3) % 10
            name_cn = _SURNAMES[index - 1] + _GIVEN[name_index]
            name_en = f"{_SURNAMES_EN[index - 1]} {_GIVEN_EN[name_index]}"
            faction = npc_factions[slot_index % 3]
            bundle.entities[npc_id] = Entity(
                id=npc_id,
                name=name_cn,
                type=EntityType.NPC,
                aliases=[name_en],
                description=(
                    f"{name_cn}（{name_en}），驻守{region_cn}的"
                    f"{bundle.entities[faction].name}成员。"
                ),
            )
            bundle.relations.append(Relation(source=npc_id, target=faction, kind="member_of"))
            bundle.relations.append(
                Relation(source=npc_id, target=f"loc_r{index}_a", kind="located_in")
            )

        for poi_slot, purpose in (("a", "补给与换马的驿点"), ("b", "瞭望与预警的岗哨")):
            poi_id = f"poi_r{index}_{poi_slot}"
            bundle.pois[poi_id] = POI(
                id=poi_id,
                name=f"{region_cn}哨点{poi_slot.upper()}",
                region_id=region_id,
                controlling_faction=npc_factions[index % 3],
                level_min=level_min,
                level_max=level_max,
                purpose=purpose,
                tags=["patrol"],
            )

        quests_in_region = 4 if index <= 6 else 3  # 6*4 + 4*3 = 36
        for quest_index in range(1, quests_in_region + 1):
            quest_id = f"quest_r{index}_q{quest_index}"
            giver_slot = "a" if quest_index % 2 == 1 else "b"
            location_slot = "a" if quest_index % 2 == 1 else "b"
            topic = _QUEST_TOPICS[(quest_index - 1) % len(_QUEST_TOPICS)]
            prerequisites = [f"quest_r{index}_q{quest_index - 1}"] if quest_index > 1 else []
            quest = Quest(
                id=quest_id,
                title=f"{region_cn}·{topic}",
                giver_npc=f"npc_r{index}_{giver_slot}",
                location=f"loc_r{index}_{location_slot}",
                objective=f"在{region_cn}完成「{topic}」，并向委托人复命。",
                prerequisites=prerequisites,
                timeline_order=index * 10 + quest_index,
                localization_keys=[f"quest.{quest_id}.objective"],
                dialogue_refs=[f"dlg_{quest_id}"],
                tags=["side"],
            )
            bundle.quests[quest_id] = quest

            bundle.dialogues[f"dlg_{quest_id}"] = DialogueRef(
                id=f"dlg_{quest_id}",
                text_key=f"dlg.{quest_id}",
                speaker_id=quest.giver_npc,
                quest_id=quest_id,
                text=f"{topic}的事就托付给你了，路上当心。",
                locale="zh-CN",
                ui_max_len=80,
            )
            for locale, text in (
                ("zh-CN", f"在{region_cn}完成「{topic}」。"),
                ("en", f"Complete '{topic}' in {region_en}."),
            ):
                row_id = f"loc_{quest_id}_{locale.replace('-', '_').lower()}"
                bundle.localized_texts[row_id] = LocalizedText(
                    id=row_id,
                    text_key=f"quest.{quest_id}.objective",
                    locale=locale,
                    text=text,
                )

    # Three quests legitimately reference event results AFTER the event occurred.
    for ref_index, (quest_id, event_id) in enumerate(
        [
            ("quest_r5_q2", "evt_mist_fire"),
            ("quest_r7_q1", "evt_xuanwu_pact"),
            ("quest_r9_q2", "evt_salt_battle"),
        ],
        start=1,
    ):
        ref_id = f"qer_{ref_index:02d}"
        bundle.quest_event_refs[ref_id] = QuestEventReference(
            id=ref_id,
            quest_id=quest_id,
            event_id=event_id,
            ref_kind=QuestEventRefKind.REFERENCES_RESULT,
        )

    bundle.terms = {
        "term_xuantie": Term(
            id="term_xuantie",
            canonical="玄铁令",
            forbidden=["黑铁令"],
            description="铁卫军团签发的通行凭证的规范名称。",
        ),
        "term_mistpact": Term(
            id="term_mistpact",
            canonical="雾隐盟",
            forbidden=["雾隐帮"],
            description="势力「雾隐盟」的规范名称。",
        ),
    }
    bundle.style_guides["style_guide"] = StyleGuide(
        body="行文使用简体中文；专有名词以术语表为准；台词长度不超过 UI 预算。",
        rules=["use-terms", "respect-ui-budget"],
    )
    return bundle


def seed_errors(clean: ContentBundle) -> tuple[ContentBundle, list[SeededError]]:
    bundle = clean.model_copy(deep=True)
    seeded: list[SeededError] = []

    def note(code: str, rule: str, target: str, text: str = "") -> None:
        seeded.append(SeededError(code=code, expected_rule=rule, target_ref=target, note=text))

    # --- reference (6)
    bundle.quests["quest_r1_q1"].giver_npc = "npc_missing_01"
    note("E01", "UNKNOWN_ENTITY_REF", "quest:quest_r1_q1", "giver -> missing npc")
    bundle.pois["poi_r2_a"].controlling_faction = "fac_missing"
    note("E02", "UNKNOWN_ENTITY_REF", "poi:poi_r2_a", "controlling faction missing")
    bundle.entities["npc_r3_b"].status = "deprecated"
    bundle.quests["quest_r3_q2"].giver_npc = "npc_r3_b"
    note("E03", "DEPRECATED_ENTITY_REF", "quest:quest_r3_q2", "giver deprecated")
    bundle.quests["quest_r4_q1"].dialogue_refs.append("dlg_missing_01")
    note("E04", "MISSING_DIALOGUE_REF", "quest:quest_r4_q1", "dialogue ref missing")
    bundle.quests["quest_r5_q1"].prerequisites.append("quest_missing_01")
    note("E05", "PREREQ_MISSING", "quest:quest_r5_q1", "prerequisite missing")
    bundle.dialogues["dlg_quest_r6_q1"].speaker_id = "npc_missing_02"
    note("E06", "UNKNOWN_ENTITY_REF", "dialogue:dlg_quest_r6_q1", "speaker missing")

    # --- graph (5)
    bundle.relations.append(Relation(source="npc_r1_a", target="npc_missing_03", kind="knows"))
    note(
        "E07",
        "MISSING_RELATION_ENDPOINT",
        "relation:npc_r1_a:knows:npc_missing_03",
        "relation target missing",
    )
    bundle.relations.append(Relation(source="npc_r2_a", target="loc_r2_a", kind="located_in"))
    note(
        "E08",
        "DUPLICATE_RELATION",
        "relation:npc_r2_a:located_in:loc_r2_a",
        "duplicated located_in",
    )
    bundle.relations.append(Relation(source="fac_trade", target="fac_mist", kind="allied_with"))
    bundle.relations.append(Relation(source="fac_trade", target="fac_mist", kind="enemy_of"))
    note(
        "E09",
        "RELATION_CONFLICT",
        "relation:fac_mist:conflict:fac_trade",
        "allied and enemy at once",
    )
    bundle.quests["quest_r7_q1"].prerequisites = ["quest_r7_q2"]
    note("E10", "PREREQ_CYCLE", "quest_prerequisites", "q1<->q2 cycle in region 7")
    for relation in bundle.relations:
        if relation.source == "loc_r8_a" and relation.kind == "controlled_by":
            relation.target = "fac_sand"
            break
    note(
        "E11",
        "FACTION_CONFLICT",
        "quest:quest_r8_q1",
        "iron giver sent into sand-controlled location",
    )

    # --- lore (6)
    bundle.quests["quest_r9_q2"].timeline_order = bundle.quests["quest_r9_q1"].timeline_order
    note("E12", "TIMELINE_VIOLATION", "quest:quest_r9_q2", "prereq not earlier")
    assert bundle.quests["quest_r10_q2"].timeline_order is not None
    bundle.quests["quest_r10_q3"].timeline_order = bundle.quests["quest_r10_q2"].timeline_order - 1
    note("E13", "TIMELINE_VIOLATION", "quest:quest_r10_q3", "order inverted in chain")
    bundle.quest_event_refs["qer_90"] = QuestEventReference(
        id="qer_90",
        quest_id="quest_r1_q2",
        event_id="evt_canglang_flood",
        ref_kind=QuestEventRefKind.REFERENCES_RESULT,
    )
    note(
        "E14",
        "EVENT_RESULT_REFERENCED_TOO_EARLY",
        "quest:quest_r1_q2",
        "references late event result",
    )
    bundle.quests["quest_r2_q1"].metadata["references_event_results"] = ["evt_ironwall_siege"]
    note(
        "E15",
        "EVENT_RESULT_REFERENCED_TOO_EARLY",
        "quest:quest_r2_q1",
        "metadata reference too early",
    )
    bundle.entities["npc_r4_b"].status = "dead"
    bundle.entities["npc_r4_b"].tags.append("active")
    note("E16", "CHARACTER_STATE_CONTRADICTION", "entity:npc_r4_b", "dead but active")
    bundle.entities["npc_r5_c"].status = "dead"
    bundle.entities["npc_r5_c"].tags.append("active")
    note("E17", "CHARACTER_STATE_CONTRADICTION", "entity:npc_r5_c", "dead but active")

    # --- region (4)
    bundle.regions["region_03"].level_min = 12
    bundle.regions["region_03"].level_max = 7
    note("E18", "REGION_LEVEL_BOUNDS_INVALID", "region:region_03", "min > max")
    assert bundle.regions["region_04"].level_max is not None
    bundle.pois["poi_r4_a"].level_max = bundle.regions["region_04"].level_max + 5
    note("E19", "POI_LEVEL_OUT_OF_BOUNDS", "poi:poi_r4_a", "level_max above region band")
    bundle.pois["poi_r5_b"].purpose = ""
    note("E20", "POI_WITHOUT_NARRATIVE_PURPOSE", "poi:poi_r5_b", "purpose emptied")
    bundle.pois["poi_r6_a"].tags.append("undead")
    note("E21", "REGION_BANNED_CONTENT_USED", "poi:poi_r6_a", "banned tag used")

    # --- pipeline / localization (4)
    bundle.quests["quest_r6_q2"].localization_keys = []
    note("E22", "MISSING_LOCALIZATION_KEY", "quest:quest_r6_q2", "keys removed")
    bundle.dialogues["dlg_quest_r7_q2"].text = "这条路上的盐车被劫了三次，" * 8
    note("E23", "TEXT_TOO_LONG_FOR_UI", "dialogue:dlg_quest_r7_q2", "text past UI budget")
    bundle.localized_texts["loc_quest_r8_q2_zh_cn"].text += "（委托人：{player}）"
    note(
        "E24",
        "PLACEHOLDER_MISMATCH",
        "dialogue_key:quest.quest_r8_q2.objective",
        "zh adds {player}",
    )
    bundle.quests["quest_r9_q1"].objective += "完成后凭黑铁令领取报酬。"
    note("E25", "TERM_INCONSISTENT", "quest:quest_r9_q1", "forbidden term used")

    return bundle, seeded


def retrieval_benchmark_queries() -> list[tuple[str, str]]:
    """30 labelled (query, expected_ref) pairs — 15 zh-CN, 15 en."""
    queries: list[tuple[str, str]] = []
    zh_targets = [
        ("npc_r1_a", "{name}是谁"),
        ("npc_r2_b", "{name}是谁"),
        ("npc_r3_c", "{name}是什么人"),
        ("npc_r6_a", "{name}效力于哪个势力"),
        ("npc_r9_b", "{name}驻守在哪里"),
        ("fac_iron", "{name}是什么势力"),
        ("fac_mist", "{name}和谁敌对"),
        ("loc_r1_a", "{name}在哪"),
        ("loc_r4_b", "{name}由谁控制"),
        ("evt_xuanwu_pact", "{name}是什么事件"),
        ("evt_salt_battle", "{name}的经过"),
        ("item_xuantie_seal", "{name}有什么用"),
        ("concept_caoyun", "{name}是什么"),
        ("quest_r1_q1", "雾脊山道的巡查烽燧任务"),
        ("quest_r5_q2", "沧浪河谷的护送盐车任务"),
    ]
    en_targets = [
        "npc_r1_a",
        "npc_r2_a",
        "npc_r3_a",
        "npc_r4_b",
        "npc_r5_c",
        "npc_r7_a",
        "npc_r8_b",
        "npc_r10_c",
        "fac_trade",
        "fac_sand",
        "loc_r2_a",
        "loc_r6_b",
        "loc_r9_a",
        "loc_r10_b",
        "fac_iron",
    ]
    bundle = build_acceptance_world()
    for entity_id, template in zh_targets:
        if entity_id.startswith("quest_"):
            queries.append((template, f"quest:{entity_id}"))
        else:
            name = bundle.entities[entity_id].name
            queries.append((template.format(name=name), f"entity:{entity_id}"))
    for entity_id in en_targets:
        alias = bundle.entities[entity_id].aliases[0]
        queries.append((f"Who or what is {alias}?", f"entity:{entity_id}"))
    return queries


def run_acceptance_evaluation(workspace: str | Path) -> AcceptanceReport:
    root = Path(workspace)
    clean_root = root / "acceptance_world"
    corrupted_root = root / "acceptance_world_seeded"

    clean = build_acceptance_world()
    ContentStore(clean_root).save(clean)
    corrupted, seeded = seed_errors(clean)
    ContentStore(corrupted_root).save(corrupted)

    checks: list[AcceptanceCheck] = []
    metrics: dict[str, Any] = {
        "entities": len(clean.entities),
        "regions": len(clean.regions),
        "quests": len(clean.quests),
        "dialogues": len(clean.dialogues),
        "localized_texts": len(clean.localized_texts),
        "seeded_errors": len(seeded),
    }

    # 1. clean world must audit to zero open issues (false-positive gate)
    clean_project = ProjectContext.open(clean_root, sqlite_path=root / "clean.sqlite")
    try:
        clean_audit = run_full_audit(clean_project, persist=False)
        open_clean = [issue for issue in clean_audit.issues if issue.status.value == "open"]
        checks.append(
            AcceptanceCheck(
                name="clean_world_zero_false_positives",
                passed=not open_clean,
                details={
                    "open_issues": [f"{issue.rule_code} {issue.target_ref}" for issue in open_clean]
                },
            )
        )

        # 2. impact recall on three known change scenarios
        analyzer = ImpactAnalyzer(clean_project.graph)
        scenarios: list[tuple[str, Change, set[str]]] = [
            (
                "delete_location",
                Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:loc_r1_a"),
                {"quest:quest_r1_q1", "entity:npc_r1_a"},
            ),
            (
                "delete_faction",
                Change(change_type=ChangeType.ENTITY_DELETE, target_ref="entity:fac_iron"),
                {"entity:npc_r1_a", "entity:npc_r2_a"},
            ),
            (
                "change_quest",
                Change(change_type=ChangeType.CONTENT_CHANGE, target_ref="quest:quest_r1_q1"),
                {"quest:quest_r1_q2"},
            ),
        ]
        missed_total: list[str] = []
        for scenario_name, change, expected in scenarios:
            result = analyzer.analyze(ChangeSet(changes=[change]))
            must = {item.target_ref for item in result.by_level(ImpactLevel.MUST_CHANGE)}
            missed = sorted(expected - must)
            missed_total.extend(f"{scenario_name}:{ref}" for ref in missed)
        checks.append(
            AcceptanceCheck(
                name="impact_recall_100",
                passed=not missed_total,
                details={"missed": missed_total},
            )
        )

        # 3. retrieval benchmark: 30 bilingual labelled queries
        queries = retrieval_benchmark_queries()
        hits = 0
        misses: list[str] = []
        for query, expected_ref in queries:
            pack = clean_project.context_builder.build(query, budget_tokens=700)
            if expected_ref in pack.refs:
                hits += 1
            else:
                misses.append(f"{query} -> {expected_ref}")
        hit_rate = hits / len(queries)
        metrics["retrieval_hit_rate"] = round(hit_rate, 4)
        checks.append(
            AcceptanceCheck(
                name="retrieval_hit_rate_gate",
                passed=hit_rate >= RETRIEVAL_HIT_RATE_GATE,
                details={"hit_rate": hit_rate, "gate": RETRIEVAL_HIT_RATE_GATE, "misses": misses},
            )
        )

        # 4. QA spot checks: grounded answers for in-world questions, refusal otherwise
        qa = LoreQAService(
            gateway=LLMGateway(
                providers={"cheap": OfflineQAProvider()},
                router=StaticRouter(mapping={"qa_answer": "cheap"}),
                cache=NoOpCache(),
                telemetry=TelemetryCollector(),
            ),
            context_builder=clean_project.context_builder,
            bundle=clean_project.bundle,
        )
        answerable = [
            clean.entities["npc_r1_a"].name + "是谁",
            "玄武之约是什么事件",
            "铁卫军团和谁敌对",
            "雾脊山道烽燧在哪",
        ]
        unanswerable = ["龙王是谁", "谁偷走了月亮"]
        qa_failures: list[str] = []
        for question in answerable:
            answer = qa.ask(question, budget_tokens=700)
            if answer.refused or not answer.citations:
                qa_failures.append(f"expected grounded answer: {question}")
        for question in unanswerable:
            answer = qa.ask(question, budget_tokens=700)
            if not answer.refused:
                qa_failures.append(f"expected refusal: {question}")
        checks.append(
            AcceptanceCheck(
                name="qa_grounded_or_refuse",
                passed=not qa_failures,
                details={"failures": qa_failures},
            )
        )
    finally:
        clean_project.close()

    # 5. seeded-error detection on the corrupted world
    corrupted_project = ProjectContext.open(corrupted_root, sqlite_path=root / "corrupted.sqlite")
    try:
        corrupted_audit = run_full_audit(corrupted_project, persist=False)
    finally:
        corrupted_project.close()
    open_issues = [issue for issue in corrupted_audit.issues if issue.status.value == "open"]
    detected: list[str] = []
    missed_errors: list[str] = []
    for error in seeded:
        matched = any(
            issue.rule_code == error.expected_rule
            and (
                issue.target_ref == error.target_ref
                or error.target_ref in (evidence.target_ref or "" for evidence in issue.evidence)
            )
            for issue in open_issues
        )
        (detected if matched else missed_errors).append(
            f"{error.code}:{error.expected_rule}:{error.target_ref}"
        )
    detection_rate = len(detected) / len(seeded)
    metrics["detection_rate"] = round(detection_rate, 4)
    metrics["detected"] = len(detected)
    checks.append(
        AcceptanceCheck(
            name="seeded_error_detection_gate",
            passed=detection_rate >= DETECTION_RATE_GATE,
            details={
                "detection_rate": detection_rate,
                "gate": DETECTION_RATE_GATE,
                "missed": missed_errors,
            },
        )
    )

    return AcceptanceReport(
        passed=all(check.passed for check in checks),
        checks=checks,
        metrics=metrics,
    )
