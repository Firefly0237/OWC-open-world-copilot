# 区域与 POI 生产规则。
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
            target_ref = f"region:{region.id}"
            if region.level_min is None or region.level_max is None:
                missing = [
                    label
                    for present, label in (
                        (region.level_min is not None, "最低等级"),
                        (region.level_max is not None, "最高等级"),
                    )
                    if not present
                ]
                missing_desc = "、".join(missing)
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"区域《{region.id}》没有填{missing_desc}——不知道这个区域适合什么等级的"
                        "玩家进入。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="level")],
                    suggested_action=(
                        f"需要策划补上具体数字:程序不知道区域《{region.id}》该设成多少级,"
                        f"请把{missing_desc}填成一个具体的等级数值。"
                    ),
                )
            elif region.level_min > region.level_max:
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"区域《{region.id}》的最低等级填的是 {region.level_min},最高等级填的是 "
                        f"{region.level_max}——下限比上限还高,等级区间是反的。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="level")],
                    suggested_action=(
                        f"把区域《{region.id}》的最低等级和最高等级对调:改成最低等级 "
                        f"{region.level_max}、最高等级 {region.level_min},区间就顺过来了。"
                    ),
                )


class MissingRegionReferenceRule:
    """POI→region reference integrity: mirrors :class:`~..rules.reference_rules.
    MissingEntityReferenceRule`'s naming/severity/shape, scoped to ``POI.region_id``.

    Neither ``reference_rules.py`` (which only walks ``poi.controlling_faction``) nor this file's
    other POI/region rules ever checked whether a POI's ``region_id`` resolves to a real region —
    a POI whose region reference is invalid silently skipped ``POILevelOutOfBoundsRule`` and
    ``RegionBannedContentRule`` (both no-op via ``ctx.bundle.regions.get(poi.region_id or "")``)
    without ever being flagged. This closes that gap: an empty region_id is "not yet specified"
    (not an error — every other rule here treats it the same way); a non-empty one that names no
    real region is a dangling reference, exactly like an unknown ``giver_npc``/``location``.

    Checked against ``ctx.bundle.regions`` directly (not the looser ``ctx.has_object``) because
    ``region_id`` specifically must resolve within the regions collection — the same collection
    ``POILevelOutOfBoundsRule``/``RegionBannedContentRule`` key off of — not any canon object.
    """

    code = "POI_UNKNOWN_REGION_REF"
    severity = Severity.ERROR
    category = Category.REGION

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for poi in ctx.bundle.pois.values():
            region_id = (poi.region_id or "").strip()
            if region_id and region_id not in ctx.bundle.regions:
                target_ref = f"poi:{poi.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"地点《{poi.id}》填的所属区域是《{region_id}》,但世界里没有这个区域"
                        "——这个地点挂不上任何一个真实存在的区域。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="region_id")],
                    suggested_action=_unknown_region_suggestion(ctx, poi.id, region_id),
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
                yield _poi_level_issue(
                    poi.id, "level_min", poi.level_min, region.id, region.level_min
                )
            if poi.level_max is not None and poi.level_max > region.level_max:
                yield _poi_level_issue(
                    poi.id, "level_max", poi.level_max, region.id, region.level_max
                )


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
                    message=(
                        f"地点《{poi.id}》的『叙事用途』是空的——没写这个地点在剧情或玩法里起"
                        "什么作用。"
                    ),
                    evidence=[Evidence(kind="field_path", target_ref=target_ref, path="purpose")],
                    suggested_action=(
                        f"需要策划补充:在地点《{poi.id}》的『叙事用途』里,写清楚这里具体做什么用"
                        "(比如触发哪个任务、揭示什么剧情、提供什么玩法),程序猜不出具体内容。"
                    ),
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
                # overlap 是小写化之后的比对结果；消息与建议里要照抄策划在标签列表里写的原文
                # 大小写，不能把它换成小写化之后的版本。
                original_case = [
                    next(tag for tag in poi.tags if tag.lower() == lowered) for lowered in overlap
                ]
                overlap_desc = "、".join(f"『{tag}』" for tag in original_case)
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"地点《{poi.id}》的标签里有 {overlap_desc},但它所在的区域《{region.id}》"
                        "明确禁止出现这些内容——地点和区域的设定互相矛盾。"
                    ),
                    evidence=[
                        Evidence(
                            kind="field_path",
                            target_ref=target_ref,
                            path="tags",
                            data={"banned": overlap},
                        )
                    ],
                    suggested_action=(
                        f"把地点《{poi.id}》标签列表里的 {overlap_desc} 删掉,和所在区域"
                        f"《{region.id}》禁止出现的内容保持一致;如果这些内容对这个地点确实是"
                        "必须的,需要策划判断是不是该换到一个允许这些内容的区域。"
                    ),
                )


def _poi_level_issue(
    poi_id: str, path: str, value: int, region_id: str, region_bound: int
) -> Issue:
    target_ref = f"poi:{poi_id}"
    if path == "level_min":
        field_desc = "最低等级"
        message = (
            f"地点《{poi_id}》标注的{field_desc}是 {value},比它所在区域《{region_id}》定的"
            f"{field_desc} {region_bound} 还低——玩家还没到区域要求的最低等级,就能先摸到这"
            "个地点,打乱了区域的等级节奏。"
        )
        suggested_action = (
            f"把地点《{poi_id}》的{field_desc}从 {value} 改成不小于 {region_bound}"
            f"(区域《{region_id}》的{field_desc}),让地点的等级范围落在区域范围以内。"
        )
    else:
        field_desc = "最高等级"
        message = (
            f"地点《{poi_id}》标注的{field_desc}是 {value},比它所在区域《{region_id}》定的"
            f"{field_desc} {region_bound} 还高——这个地点的内容等级超出了所在区域设定的上限。"
        )
        suggested_action = (
            f"把地点《{poi_id}》的{field_desc}从 {value} 改成不超过 {region_bound}"
            f"(区域《{region_id}》的{field_desc}),让地点的等级范围落在区域范围以内。"
        )
    return Issue(
        rule_code=POILevelOutOfBoundsRule.code,
        severity=POILevelOutOfBoundsRule.severity,
        category=POILevelOutOfBoundsRule.category,
        target_ref=target_ref,
        message=message,
        evidence=[
            Evidence(
                kind="field_path",
                target_ref=target_ref,
                path=path,
                data={"region_id": region_id, "value": value},
            )
        ],
        suggested_action=suggested_action,
    )


def _unknown_region_suggestion(ctx: AuditContext, poi_id: str, region_id: str) -> str:
    """世界里已有的区域 id 是确定性的真实数据，直接列出来给策划挑，不用编一个假 id。"""
    existing = sorted(ctx.bundle.regions.keys())
    if not existing:
        return (
            f"世界里目前还没有任何区域,需要先新建一个 id 为《{region_id}》的区域,再把地点"
            f"《{poi_id}》的所属区域指过去。"
        )
    existing_desc = "、".join(f"《{rid}》" for rid in existing)
    return (
        f"把地点《{poi_id}》的所属区域改成世界里已有的区域之一,比如 {existing_desc};"
        f"如果《{region_id}》确实应该存在,就去新建这个区域。"
    )
