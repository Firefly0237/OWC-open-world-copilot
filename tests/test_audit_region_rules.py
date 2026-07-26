from __future__ import annotations

import re

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.region_rules import (
    MissingRegionReferenceRule,
    POILevelOutOfBoundsRule,
    POIWithoutNarrativePurposeRule,
    RegionBannedContentRule,
    RegionLevelBoundsRule,
)
from owcopilot.content.models import POI, ContentBundle, RegionBrief

# -- 可执行报告改造：中文消息 + 实例化建议动作的机械核验 -------------------------------------------
# 三条机械检查（计划 §三）：消息是中文、建议含这一条实例的具体字段名或值、消息与建议都不含代码
# 标识符。对象 id（region/poi 的 id）与内容数据本身（标签值等）不算代码标识符，允许出现。

_HAS_CJK = re.compile(r"[一-鿿]")

_FORBIDDEN_IDENTIFIERS = (
    "level_min",
    "level_max",
    "region_id",
    "banned_content",
    "purpose",
    "target_ref",
)


def _assert_planner_readable(text: str) -> None:
    assert _HAS_CJK.search(text), f"不是中文: {text!r}"
    for identifier in _FORBIDDEN_IDENTIFIERS:
        assert identifier not in text, f"消息/建议里混进了代码标识符 {identifier!r}: {text!r}"


def test_region_level_bounds_rule_flags_missing_or_invalid_bounds() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={
                "region_missing": RegionBrief(id="region_missing", name="Missing"),
                "region_invalid": RegionBrief(
                    id="region_invalid",
                    name="Invalid",
                    level_min=10,
                    level_max=5,
                ),
            }
        )
    )

    issues = list(RegionLevelBoundsRule().check(ctx))

    assert [issue.rule_code for issue in issues] == [
        "REGION_LEVEL_BOUNDS_INVALID",
        "REGION_LEVEL_BOUNDS_INVALID",
    ]


def test_poi_level_out_of_bounds_rule_flags_poi_bounds() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={"region_a": RegionBrief(id="region_a", name="A", level_min=5, level_max=10)},
            pois={"poi_a": POI(id="poi_a", name="A", region_id="region_a", level_min=4)},
        )
    )

    issues = list(POILevelOutOfBoundsRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "POI_LEVEL_OUT_OF_BOUNDS"


def test_poi_level_out_of_bounds_rule_skips_invalid_region_bounds() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={
                "region_invalid": RegionBrief(
                    id="region_invalid",
                    name="Invalid",
                    level_min=10,
                    level_max=5,
                )
            },
            pois={
                "poi_a": POI(
                    id="poi_a",
                    name="A",
                    region_id="region_invalid",
                    level_min=1,
                    level_max=20,
                )
            },
        )
    )

    issues = list(POILevelOutOfBoundsRule().check(ctx))

    assert issues == []


def test_poi_without_narrative_purpose_rule_flags_empty_purpose() -> None:
    ctx = AuditContext.from_bundle(ContentBundle(pois={"poi_a": POI(id="poi_a", name="A")}))

    issues = list(POIWithoutNarrativePurposeRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "POI_WITHOUT_NARRATIVE_PURPOSE"


def test_missing_region_reference_rule_flags_dangling_region_id() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={"region_a": RegionBrief(id="region_a", name="A")},
            pois={
                "poi_ghost": POI(id="poi_ghost", name="Ghost", region_id="region_missing"),
            },
        )
    )

    issues = list(MissingRegionReferenceRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "POI_UNKNOWN_REGION_REF"
    assert issues[0].target_ref == "poi:poi_ghost"
    assert issues[0].evidence[0].path == "region_id"


def test_missing_region_reference_rule_does_not_flag_valid_region() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={"region_a": RegionBrief(id="region_a", name="A")},
            pois={"poi_a": POI(id="poi_a", name="A", region_id="region_a")},
        )
    )

    issues = list(MissingRegionReferenceRule().check(ctx))

    assert issues == []


def test_missing_region_reference_rule_does_not_flag_unspecified_region() -> None:
    # An empty region_id means "not yet assigned", not a dangling reference — every other POI
    # rule in this file treats a blank region_id the same way (skip, not flag).
    ctx = AuditContext.from_bundle(
        ContentBundle(pois={"poi_bare": POI(id="poi_bare", name="Bare")})
    )

    issues = list(MissingRegionReferenceRule().check(ctx))

    assert issues == []


def test_region_banned_content_rule_flags_poi_tags() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={
                "region_a": RegionBrief(
                    id="region_a",
                    name="A",
                    banned_content=["undead"],
                )
            },
            pois={
                "poi_a": POI(
                    id="poi_a",
                    name="A",
                    region_id="region_a",
                    tags=["undead"],
                )
            },
        )
    )

    issues = list(RegionBannedContentRule().check(ctx))

    assert len(issues) == 1
    assert issues[0].rule_code == "REGION_BANNED_CONTENT_USED"


# -- 中文消息 + 实例化建议动作 ----------------------------------------------------------------


def test_region_level_bounds_rule_missing_bounds_asks_a_human_for_a_number() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(regions={"region_missing": RegionBrief(id="region_missing", name="Missing")})
    )

    (issue,) = list(RegionLevelBoundsRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "region_missing" in issue.message
    assert "最低等级" in issue.message and "最高等级" in issue.message
    # 没有可用的具体数字——如实说明需要人补，不编一个假数字。
    assert "需要策划补上" in issue.suggested_action


def test_region_level_bounds_rule_swapped_bounds_suggests_the_swap_with_real_numbers() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={
                "region_invalid": RegionBrief(
                    id="region_invalid", name="Invalid", level_min=10, level_max=5
                )
            }
        )
    )

    (issue,) = list(RegionLevelBoundsRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    # 这一条实例真实的数字（10 和 5），不是泛泛的规则描述。
    assert "10" in issue.suggested_action
    assert "5" in issue.suggested_action


def test_missing_region_reference_rule_suggestion_lists_real_existing_regions() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={
                "region_a": RegionBrief(id="region_a", name="A"),
                "region_b": RegionBrief(id="region_b", name="B"),
            },
            pois={"poi_ghost": POI(id="poi_ghost", name="Ghost", region_id="region_missing")},
        )
    )

    (issue,) = list(MissingRegionReferenceRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "poi_ghost" in issue.message and "region_missing" in issue.message
    # 建议里列出的是世界里真实存在的区域 id，不是编出来的。
    assert "region_a" in issue.suggested_action
    assert "region_b" in issue.suggested_action


def test_missing_region_reference_rule_suggestion_when_no_region_exists_at_all() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            pois={"poi_ghost": POI(id="poi_ghost", name="Ghost", region_id="region_missing")},
        )
    )

    (issue,) = list(MissingRegionReferenceRule().check(ctx))

    _assert_planner_readable(issue.suggested_action)
    assert "新建" in issue.suggested_action
    assert "region_missing" in issue.suggested_action


def test_poi_level_out_of_bounds_rule_suggestion_has_both_real_numbers() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={"region_a": RegionBrief(id="region_a", name="A", level_min=5, level_max=10)},
            pois={"poi_a": POI(id="poi_a", name="A", region_id="region_a", level_min=4)},
        )
    )

    (issue,) = list(POILevelOutOfBoundsRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "poi_a" in issue.message and "region_a" in issue.message
    # POI 自己的值（4）和它必须落进去的区域边界（5）都要在建议里出现。
    assert "4" in issue.suggested_action
    assert "5" in issue.suggested_action


def test_poi_without_narrative_purpose_rule_asks_a_human_for_content() -> None:
    ctx = AuditContext.from_bundle(ContentBundle(pois={"poi_a": POI(id="poi_a", name="A")}))

    (issue,) = list(POIWithoutNarrativePurposeRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "poi_a" in issue.message
    # 叙事内容编不出来——如实说明需要策划补，不假装给了一个具体用途。
    assert "需要策划补充" in issue.suggested_action


def test_region_banned_content_rule_suggestion_keeps_original_tag_case() -> None:
    ctx = AuditContext.from_bundle(
        ContentBundle(
            regions={"region_a": RegionBrief(id="region_a", name="A", banned_content=["undead"])},
            pois={
                "poi_a": POI(
                    id="poi_a", name="A", region_id="region_a", tags=["Undead"]
                )  # 大写写法：消息/建议必须照抄原文
            },
        )
    )

    (issue,) = list(RegionBannedContentRule().check(ctx))

    _assert_planner_readable(issue.message)
    _assert_planner_readable(issue.suggested_action)
    assert "Undead" in issue.message
    assert "Undead" in issue.suggested_action
    assert "poi_a" in issue.suggested_action and "region_a" in issue.suggested_action
