"""Region and POI production rules."""

from __future__ import annotations

from collections.abc import Iterable

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity


class RegionLevelBoundsRule:
    code = "REGION_LEVEL_BOUNDS_INVALID"
    severity = Severity.ERROR
    category = Category.REGION

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for region in ctx.bundle.regions.values():
            if region.level_min is None or region.level_max is None:
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=f"region:{region.id}",
                    message=f"Region '{region.id}' is missing level bounds",
                    evidence=[
                        Evidence(kind="field_path", target_ref=f"region:{region.id}", path="level")
                    ],
                )
            elif region.level_min > region.level_max:
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=f"region:{region.id}",
                    message=f"Region '{region.id}' has min level greater than max level",
                    evidence=[
                        Evidence(kind="field_path", target_ref=f"region:{region.id}", path="level")
                    ],
                )


class POILevelOutOfBoundsRule:
    code = "POI_LEVEL_OUT_OF_BOUNDS"
    severity = Severity.ERROR
    category = Category.REGION

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for poi in ctx.bundle.pois.values():
            region = ctx.bundle.regions.get(poi.region_id or "")
            if region is None or region.level_min is None or region.level_max is None:
                continue
            if region.level_min > region.level_max:
                continue
            if poi.level_min is not None and poi.level_min < region.level_min:
                yield _poi_level_issue(poi.id, "level_min", poi.level_min, region.id)
            if poi.level_max is not None and poi.level_max > region.level_max:
                yield _poi_level_issue(poi.id, "level_max", poi.level_max, region.id)


class POIWithoutNarrativePurposeRule:
    code = "POI_WITHOUT_NARRATIVE_PURPOSE"
    severity = Severity.WARNING
    category = Category.REGION

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for poi in ctx.bundle.pois.values():
            if not poi.purpose.strip():
                target_ref = f"poi:{poi.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"POI '{poi.id}' has no narrative purpose",
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="purpose")],
                )


class RegionBannedContentRule:
    code = "REGION_BANNED_CONTENT_USED"
    severity = Severity.ERROR
    category = Category.REGION

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for poi in ctx.bundle.pois.values():
            region = ctx.bundle.regions.get(poi.region_id or "")
            if region is None:
                continue
            banned = {item.lower() for item in region.banned_content}
            used = {tag.lower() for tag in poi.tags}
            overlap = sorted(banned & used)
            if overlap:
                target_ref = f"poi:{poi.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=f"POI '{poi.id}' uses banned region content: {', '.join(overlap)}",
                    evidence=[
                        Evidence(
                            kind="field_path",
                            target_ref=target_ref,
                            path="tags",
                            data={"banned": overlap},
                        )
                    ],
                )


def _poi_level_issue(poi_id: str, path: str, value: int, region_id: str) -> Issue:
    target_ref = f"poi:{poi_id}"
    return Issue(
        rule_code=POILevelOutOfBoundsRule.code,
        severity=POILevelOutOfBoundsRule.severity,
        category=POILevelOutOfBoundsRule.category,
        target_ref=target_ref,
        message=f"POI '{poi_id}' {path}={value} is outside region '{region_id}' bounds",
        evidence=[
            Evidence(
                kind="field_path",
                target_ref=target_ref,
                path=path,
                data={"region_id": region_id, "value": value},
            )
        ],
    )
