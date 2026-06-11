from __future__ import annotations

from owcopilot.audit.context import AuditContext
from owcopilot.audit.rules.region_rules import (
    POILevelOutOfBoundsRule,
    POIWithoutNarrativePurposeRule,
    RegionBannedContentRule,
    RegionLevelBoundsRule,
)
from owcopilot.content.models import POI, ContentBundle, RegionBrief


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
