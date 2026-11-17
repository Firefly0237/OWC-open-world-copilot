# N3.1 素材泄漏确定性检测：草稿 × 被引素材原文的逐字重合 + 专名照搬。
"""验收 ``owcopilot.inspiration.leakage`` 与它在创世链路上的接线。

这一条**不是审计规则**：默认规则表仍是 35 条，检测结论作为 ``reference_report`` 的一个字段
（``ReferenceReportItem.leakage``）交给人看，不产出 Issue、不是 ERROR、不拦任何东西。测试也照这
个定位写——断言的是"检测报告字段"的取值与文案，不是审计报告。

测试只组装输入、断言产出：n-gram/专名的判定逻辑一律走生产代码
（``detect_reference_leakage`` / ``worldgen.service._reference_report``），这里不复写一份。
"""

from __future__ import annotations

import re
import sqlite3

from owcopilot.app.actions import (
    add_reference_action,
    run_world_seed_action,
    search_references_action,
)
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest, QuestStage
from owcopilot.content.store import ContentStore
from owcopilot.inspiration.leakage import detect_reference_leakage
from owcopilot.inspiration.models import Provenance
from owcopilot.inspiration.store import ReferenceStore
from owcopilot.retrieval.models import ContextPack, RetrievalHit
from owcopilot.storage import SQLiteStore
from owcopilot.worldgen.service import _reference_report

_CJK = re.compile(r"[一-鿿]")

# 检测结论是给策划看的，不许把字段名/类名漏进去
# （与 tests/domain/test_audit_report_wording.py 同口径）。
_BANNED_IDENTIFIERS = (
    "verbatim_spans",
    "leaked_names",
    "threshold_label",
    "reference_report",
    "source_ref",
    "allowed_uses",
    "provenance",
    "third_party_reference",
    "own_ip",
    "ReferenceLeakage",
    "detect_reference_leakage",
    "self.",
)


# 一段"素材原文"。下面每个草稿变体都拿它当被引素材。
MATERIAL = (
    "黄昏时分，商队沿着盐碱地的边缘缓缓前行，骆驼的铃声一路撞碎在风里。"
    "领队的老人不断回头张望，他知道再往北走两日，就会进入那片没有水源的荒原。"
    "队伍里有一个年轻的抄书人，他把每一天见到的地貌都记在羊皮卷上。"
    "老人对他说，这些字将来会比货物值钱，因为走过这条路的人，十个里活不下来三个。"
)

# 素材里反复点名的专名——真实素材就是这样（人名会在一段里出现好几次）。
NAMED_MATERIAL = (
    "沙金娘子在盐碱地上开出第一条商路。三年之后，沙金娘子把商路卖给了驼铃会，"
    "驼铃会又把它转手给北面的城主。人们仍旧叫它沙金道，尽管沙金娘子早已不在。"
)


def _chunk_hit(body: str, *, provenance: str = "", ref: str = "reference_chunk:c1") -> RetrievalHit:
    return RetrievalHit(
        ref=ref,
        object_type="reference_chunk",
        title="荒原商路手记 / 片段一",
        body=body,
        score=0.9,
        source="reference",
        metadata={"source_title": "荒原商路手记", "provenance": provenance},
    )


def _draft_with_description(description: str, *, name: str = "抄书人札记") -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_scribe": Entity(
                id="npc_scribe", name=name, type=EntityType.NPC, description=description
            )
        }
    )


def _detect(draft: ContentBundle, material: str, provenance: str | Provenance):
    """走生产接线（``_reference_report``）拿检测结论，而不是绕过它直接调检测函数。"""
    pack = ContextPack(
        query="q",
        budget_tokens=512,
        hits=[_chunk_hit(material, provenance=str(getattr(provenance, "value", provenance)))],
    )
    rows = _reference_report(
        {}, pack, "灵感参考", draft=draft, canon=ContentBundle(), human_input=""
    )
    assert len(rows) == 1
    assert rows[0].leakage is not None
    return rows[0].leakage


# --------------------------------------------------------------------------------------------------
# 1) 抄原文进草稿 → 命中
# --------------------------------------------------------------------------------------------------
def test_verbatim_sentence_copied_into_the_draft_is_detected() -> None:
    draft = _draft_with_description(
        "他的来历不明。队伍里有一个年轻的抄书人，他把每一天见到的地貌都记在羊皮卷上。"
    )

    leakage = _detect(draft, MATERIAL, Provenance.OWN_IP)  # 连自有素材这一档都拦得住

    assert leakage.checked is True
    assert leakage.detected is True
    assert leakage.verbatim_spans
    assert "队伍里有一个年轻的抄书人" in leakage.verbatim_spans[0]


def test_copied_latin_sentence_is_detected_by_the_word_shingle() -> None:
    material = (
        "The old guide told him those words would one day be worth more than the cargo, "
        "because of every ten men who walked this road, fewer than three came back alive."
    )
    draft = _draft_with_description(
        "A caravan scribe. Those words would one day be worth more than the cargo, he said."
    )

    leakage = _detect(draft, material, Provenance.OWN_IP)

    assert leakage.detected is True
    # 片段过长时只展示开头，但要把总长度带出来（截断不能把"抄了多长"也截掉）。
    assert leakage.verbatim_spans[0].startswith("those words would one day be worth")
    assert "共 54 字" in leakage.verbatim_spans[0]


# --------------------------------------------------------------------------------------------------
# 2) 误报边界：改写转述 / 同题材另写 / 沿用正典专名 / 人自己写的名字 / 单字名 → 都不命中
# --------------------------------------------------------------------------------------------------
def test_hand_reworded_draft_does_not_trip_the_check() -> None:
    """改写转述——同一件事、同样的意思，换了自己的话说。这是最该放过的那一类。"""
    draft = _draft_with_description(
        "傍晚，一支商队贴着盐地边走。带队的老者时时回望，因为再走两天就没有水了。"
        "队中有个年轻文书，逐日把沿途地形画进皮卷：会挪动的沙丘、雨后才出水的井眼。"
    )

    leakage = _detect(draft, MATERIAL, Provenance.THIRD_PARTY_REFERENCE)  # 最严的一档

    assert leakage.checked is True
    assert leakage.detected is False
    assert leakage.verbatim_spans == []


def test_unrelated_prose_on_the_same_topic_does_not_trip_the_check() -> None:
    """同题材另写：写的是沙漠商路这件事本身，不是这份素材。"""
    draft = _draft_with_description(
        "沙漠商路上的向导多半靠星象辨方向，白天则看沙丘的走向与骆驼刺的疏密。"
        "补给点之间通常隔着三到五日的路程，一旦错过就只能宰杀牲口取水。"
    )

    leakage = _detect(draft, MATERIAL, Provenance.THIRD_PARTY_REFERENCE)

    assert leakage.detected is False


def test_a_name_the_world_already_uses_is_grounding_not_copying() -> None:
    """既有正典里已经有这个名字：草稿沿用它是接地，素材里凑巧也有不算照搬。"""
    canon = ContentBundle(
        entities={
            "npc_shajin": Entity(id="npc_shajin", name="沙金娘子", type=EntityType.NPC),
        }
    )
    draft = _draft_with_description("她重走了当年那条商路。", name="沙金娘子")
    pack = ContextPack(
        query="q",
        budget_tokens=512,
        hits=[_chunk_hit(NAMED_MATERIAL, provenance=Provenance.THIRD_PARTY_REFERENCE.value)],
    )

    rows = _reference_report({}, pack, "灵感参考", draft=draft, canon=canon, human_input="")

    assert rows[0].leakage is not None
    assert rows[0].leakage.detected is False
    assert rows[0].leakage.leaked_names == []


def test_a_name_the_creator_typed_in_the_brief_is_never_leakage() -> None:
    """人在需求里自己点名要这个角色——素材里也有，那是人的原意，不是模型照搬。"""
    draft = _draft_with_description("她重走了当年那条商路。", name="沙金娘子")
    pack = ContextPack(
        query="q",
        budget_tokens=512,
        hits=[_chunk_hit(NAMED_MATERIAL, provenance=Provenance.THIRD_PARTY_REFERENCE.value)],
    )

    rows = _reference_report(
        {},
        pack,
        "灵感参考",
        draft=draft,
        canon=ContentBundle(),
        human_input="核心想法：写一个关于沙金娘子的商路故事",
    )

    assert rows[0].leakage is not None
    assert rows[0].leakage.leaked_names == []


def test_single_character_names_are_too_short_to_count_as_leakage() -> None:
    """单字名撞上素材是巧合，不是照搬——离线创世的真实草稿里就有这种一字名。"""
    draft = _draft_with_description("无名之辈。", name="风")
    material = "风起时，商队停在风口，风沙盖住了车辙。"

    leakage = _detect(draft, material, Provenance.THIRD_PARTY_REFERENCE)

    assert leakage.leaked_names == []


# --------------------------------------------------------------------------------------------------
# 3) 来源归属决定严格度
# --------------------------------------------------------------------------------------------------
_TEN_CHAR_MATERIAL = "三方争夺枯竭资源，边境小镇最先付出代价，商路随之断绝。"
_TEN_CHAR_DRAFT = "群岛的秩序崩塌时，边境小镇最先付出代价，随后是海上的商会。"


def test_third_party_material_is_checked_more_strictly_than_owned_material() -> None:
    """同一段 10 字重合：自有素材按常规档（12 字）放过，第三方素材按严格档（8 字）报出来。"""
    draft = _draft_with_description(_TEN_CHAR_DRAFT)

    owned = _detect(draft, _TEN_CHAR_MATERIAL, Provenance.OWN_IP)
    licensed = _detect(draft, _TEN_CHAR_MATERIAL, Provenance.LICENSED)
    third_party = _detect(draft, _TEN_CHAR_MATERIAL, Provenance.THIRD_PARTY_REFERENCE)

    assert owned.detected is False
    assert licensed.detected is False
    assert third_party.detected is True
    assert "边境小镇最先付出代价" in third_party.verbatim_spans[0]


def test_unknown_provenance_falls_back_to_the_strictest_tier() -> None:
    """归属拿不到时按最保守的一档处理，与素材模型的默认值同向——不因为不知道就放宽。"""
    draft = _draft_with_description(_TEN_CHAR_DRAFT)

    leakage = _detect(draft, _TEN_CHAR_MATERIAL, "")

    assert leakage.detected is True


# --------------------------------------------------------------------------------------------------
# 4) 专名照搬
# --------------------------------------------------------------------------------------------------
def test_a_proper_noun_lifted_from_third_party_material_is_detected() -> None:
    draft = _draft_with_description("盐路上最出名的行商。", name="沙金娘子")

    leakage = _detect(draft, NAMED_MATERIAL, Provenance.THIRD_PARTY_REFERENCE)

    assert leakage.detected is True
    assert leakage.leaked_names == ["沙金娘子"]


# --------------------------------------------------------------------------------------------------
# 5) 没拿到原文就说没查，不假装查过
# --------------------------------------------------------------------------------------------------
def test_a_source_with_no_text_reports_not_checked_rather_than_clean() -> None:
    draft = _draft_with_description("随便写点什么。")

    leakage = _detect(draft, "", Provenance.THIRD_PARTY_REFERENCE)

    assert leakage.checked is False
    assert leakage.detected is False
    assert "没有做" in leakage.message


def test_clean_result_says_what_was_checked_instead_of_claiming_compliance() -> None:
    """零命中的文案必须说清"按哪一档查的、这不等于完全没借用"，否则又变成一句不可核查的断言。"""
    draft = _draft_with_description("完全另起炉灶的一段设定，与素材无关。")

    leakage = _detect(draft, MATERIAL, Provenance.THIRD_PARTY_REFERENCE)

    assert leakage.detected is False
    assert leakage.threshold_label
    assert leakage.threshold_label in leakage.message
    assert "不等于" in leakage.message


# --------------------------------------------------------------------------------------------------
# 6) 文案门：中文、给下一步、不含代码标识符
# --------------------------------------------------------------------------------------------------
def test_leakage_messages_are_planner_readable_chinese() -> None:
    hit_draft = _draft_with_description(
        "队伍里有一个年轻的抄书人，他把每一天见到的地貌都记在羊皮卷上。", name="沙金娘子"
    )
    messages = [
        _detect(hit_draft, MATERIAL, Provenance.THIRD_PARTY_REFERENCE).message,
        _detect(_draft_with_description("另写的一段。"), MATERIAL, Provenance.OWN_IP).message,
        _detect(_draft_with_description("另写的一段。"), "", Provenance.OWN_IP).message,
    ]
    for message in messages:
        assert _CJK.search(message), f"检测结论不是中文：{message!r}"
        for token in _BANNED_IDENTIFIERS:
            assert token not in message, f"文案里混进了代码标识符 {token!r}：{message!r}"
    # 命中的那条要给出下一步，而不是只报一个结论。
    assert "改写" in messages[0] and "来源归属" in messages[0]


# --------------------------------------------------------------------------------------------------
# 7) 端到端：真跑一遍离线创世，整份草稿对每一条被引素材都是"查过且干净"
# --------------------------------------------------------------------------------------------------
def test_offline_world_seed_reports_a_real_check_on_every_cited_source(tmp_path) -> None:
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(
        root,
        title="三方争资源",
        text=(
            "一个可用的结构：三个势力争夺一种正在枯竭的资源。一方掌握基础设施，"
            "一方守护古老仪式，一方贩卖情报。资源枯竭的那一天就是摊牌的那一天。"
        ),
        allowed_uses=["inspiration"],
    )

    result = run_world_seed_action(
        root,
        brief={
            "idea": "一个靠蒸汽巨树供能的边境群岛",
            "reference_query": "三个势力争夺枯竭资源",
            "faction_count": 3,
            "region_count": 2,
            "npc_count": 4,
            "quest_count": 3,
            "term_count": 3,
        },
    )

    rows = result["reference_report"]
    assert rows
    for row in rows:
        leakage = row["leakage"]
        assert leakage["checked"] is True, "被引素材有原文，就必须真的查一遍"
        assert leakage["detected"] is False, leakage["message"]
        assert leakage["verbatim_spans"] == []
        assert leakage["leaked_names"] == []
        # 依旧不许替模型断言"未复用原文"——那是 transformation 字段的纪律，没被这次改动放松。
        assert "未复用" not in row["transformation"]


def test_leakage_check_covers_quest_prose_not_only_entity_descriptions() -> None:
    """草稿的自由文本面不止实体简介：任务目标与阶段摘要照抄同样要抓出来。"""
    draft = ContentBundle(
        quests={
            "q1": Quest(
                id="q1",
                title="押运",
                objective="护送商队穿过盐碱地。",
                stages=[
                    QuestStage(
                        id="s1",
                        summary="队伍里有一个年轻的抄书人，他把每一天见到的地貌都记在羊皮卷上。",
                    )
                ],
            )
        }
    )

    leakage = _detect(draft, MATERIAL, Provenance.OWN_IP)

    assert leakage.detected is True


# --------------------------------------------------------------------------------------------------
# 8) 来源归属要真的到得了检测端：落库 → 检索命中 → 旧库原地升级
# --------------------------------------------------------------------------------------------------
def test_provenance_reaches_the_retrieval_hit_that_the_check_reads(tmp_path) -> None:
    """严格度是按来源归属分档的，所以归属必须一路带到检索结果上，否则分档等于没有。"""
    root = tmp_path / "content"
    ContentStore(root).save(ContentBundle())
    add_reference_action(root, title="自有稿", text="三方争夺枯竭资源的结构。", provenance="own_ip")
    add_reference_action(root, title="第三方书", text="商队沿着盐碱地的边缘缓缓前行。")

    hits = search_references_action(root, query="盐碱地 商队 资源")["hits"]

    by_title = {h["metadata"]["source_title"]: h["metadata"]["provenance"] for h in hits}
    assert by_title["自有稿"] == "own_ip"
    assert by_title["第三方书"] == "third_party_reference"


def test_legacy_reference_db_upgrades_and_self_heals_the_provenance(tmp_path) -> None:
    """[迁移] 早于本次改动的运行库没有归属列：开库要原地加列（历史行拿最保守的默认值），
    下一次与磁盘素材同步时再按磁盘上的声明改正过来。"""
    db_path = tmp_path / "legacy.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        """
        CREATE TABLE reference_sources (
            world_id TEXT NOT NULL DEFAULT 'default',
            version TEXT NOT NULL DEFAULT 'v1',
            id TEXT NOT NULL,
            title TEXT NOT NULL,
            source_type TEXT NOT NULL,
            original_filename TEXT,
            allowed_uses_json TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            metadata_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (world_id, version, id)
        )
        """
    )
    conn.execute(
        "INSERT INTO reference_sources VALUES "
        "('default','v1','ref_old','旧素材','uploaded_file',NULL,'[\"inspiration\"]',"
        "'hash','{}','2026-01-01T00:00:00Z')"
    )
    conn.commit()
    conn.close()

    store = SQLiteStore(str(db_path))
    try:
        info = store.conn.execute("PRAGMA table_info(reference_sources)")
        assert "provenance" in {str(row["name"]) for row in info}
        legacy = {s["id"]: s["provenance"] for s in store.list_reference_sources()}
        assert legacy["ref_old"] == "third_party_reference"  # 不知道就按最保守的算

        # 磁盘上的素材声明为自有 —— 下一次同步必须把库里的历史值改正，而不是永远停在默认值。
        reference_store = ReferenceStore(tmp_path / "content")
        reference_store.add_text(title="自有稿", text="自己写的一段设定。", provenance="own_ip")
        reference_store.sync_index(store)

        healed = {s["title"]: s["provenance"] for s in store.list_reference_sources()}
        assert healed["自有稿"] == "own_ip"
    finally:
        store.close()


def test_detector_is_callable_without_the_worldgen_seam() -> None:
    """检测本身不依赖创世链路——批次 4 要把它接到别的生成链路上时，直接调这一个入口。"""
    leakage = detect_reference_leakage(
        draft_texts=["队伍里有一个年轻的抄书人，他把每一天见到的地貌都记在羊皮卷上。"],
        candidate_names=[],
        reference_text=MATERIAL,
        source_title="荒原商路手记",
        provenance=Provenance.OWN_IP,
    )

    assert leakage.detected is True
    assert leakage.verbatim_spans
