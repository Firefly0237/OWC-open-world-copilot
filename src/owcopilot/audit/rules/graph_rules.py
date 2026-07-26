# 图与关系一致性规则（对称性/孤儿节点/矛盾指向等）。
"""Graph and relationship consistency rules."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

import networkx as nx

from ..context import AuditContext
from ..models import Category, Evidence, Issue, Severity

_SIDE_LABEL = {"source": "来源", "target": "目标"}


class MissingRelationEndpointRule:
    code = "MISSING_RELATION_ENDPOINT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        for relation in ctx.bundle.relations:
            for side, entity_id in (("source", relation.source), ("target", relation.target)):
                if not ctx.has_object(entity_id):
                    yield Issue(
                        rule_code=self.code,
                        severity=self.severity,
                        category=self.category,
                        target_ref=f"relation:{relation.source}:{relation.kind}:{relation.target}",
                        message=(
                            f"世界关系《{relation.source} —{relation.kind}→ {relation.target}》里，"
                            f"{_SIDE_LABEL[side]}对象 '{entity_id}' 并不存在 —— 这条关系挂在一个"
                            "不存在的对象上，没法生效。"
                        ),
                        # 与修复器 _fix_missing_relation_endpoint 一致：它会把这整条关系删掉。
                        suggested_action=(
                            f"删除这整条世界关系《{relation.source} {relation.kind} "
                            f"{relation.target}》；如果 '{entity_id}' 只是改了 id，把这一端改成"
                            "正确的对象 id 也可以。"
                        ),
                        evidence=[
                            Evidence(
                                kind="relation",
                                relation=(relation.source, relation.kind, relation.target),
                                path=side,
                            )
                        ],
                    )


class DuplicateRelationRule:
    code = "DUPLICATE_RELATION"
    severity = Severity.WARNING
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        groups: dict[tuple[str, str, str], list[str | None]] = {}
        for relation in ctx.bundle.relations:
            key = (relation.source, relation.kind, relation.target)
            groups.setdefault(key, []).append(
                relation.source_ref.path if relation.source_ref else None
            )
        for (source, kind, target), source_paths in groups.items():
            counts = Counter(source_paths)
            repeated_in_source = max(counts.values(), default=0)
            count = len(source_paths)
            if repeated_in_source <= 1:
                continue
            if count > 1:
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=f"relation:{source}:{kind}:{target}",
                    message=(
                        f"世界关系《{source} —{kind}→ {target}》重复写了 {count} 次 —— 这是同一条"
                        "设定被重复录入了。"
                    ),
                    # 与修复器 _fix_duplicate_relation 一致：保留第一条，删掉其余重复的。
                    suggested_action=(
                        f"只保留第一条《{source} {kind} {target}》记录，删掉其余 "
                        f"{count - 1} 条重复的。"
                    ),
                    evidence=[Evidence(kind="relation", relation=(source, kind, target))],
                )


class RelationshipConflictRule:
    code = "RELATION_CONFLICT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        allied = {
            _pair(r.source, r.target) for r in ctx.bundle.relations if r.kind == "allied_with"
        }
        enemies = {_pair(r.source, r.target) for r in ctx.bundle.relations if r.kind == "enemy_of"}
        for source, target in sorted(allied & enemies):
            yield Issue(
                rule_code=self.code,
                severity=self.severity,
                category=self.category,
                target_ref=f"relation:{source}:conflict:{target}",
                message=(
                    f"对象 '{source}' 和 '{target}' 之间，世界关系里同时写着结盟（allied_with）"
                    "和敌对（enemy_of）两条记录 —— 这两条设定互相矛盾，不可能同时成立。"
                ),
                # 没有自动修复器：留哪条、删哪条属于设定判断，只给出两个具体候选，不代为决定。
                suggested_action=(
                    f"世界关系里 '{source}' 与 '{target}' 之间的结盟（allied_with）和敌对"
                    "（enemy_of）两条记录只能留一条；具体删哪一条需要策划确认设定。"
                ),
                evidence=[
                    Evidence(kind="relation", relation=(source, "allied_with", target)),
                    Evidence(kind="relation", relation=(source, "enemy_of", target)),
                ],
            )


class PrerequisiteCycleRule:
    code = "PREREQ_CYCLE"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        graph = nx.DiGraph()
        for quest in ctx.bundle.quests.values():
            for prereq in quest.prerequisites:
                graph.add_edge(quest.id, prereq)
        try:
            cycle = nx.find_cycle(graph, orientation="original")
        except nx.NetworkXNoCycle:
            return
        refs = [str(edge[0]) for edge in cycle]
        chain = " → ".join(f"《{ref}》" for ref in refs) + f" → 《{refs[0]}》"
        yield Issue(
            rule_code=self.code,
            severity=self.severity,
            category=self.category,
            target_ref="quest_prerequisites",
            message=(
                f"这几个任务的前置关系形成了一个环：{chain} —— 谁都要等对方先解锁，结果谁都"
                "解锁不了。"
            ),
            # 没有自动修复器：断环删哪条边属于任务先后顺序的设计判断，给出打断环的一种
            # 具体做法（删掉闭合环的那条边），并如实说明这只是候选之一。
            suggested_action=(
                f"打开任务《{refs[-1]}》，从它的前置任务列表里删掉对《{refs[0]}》的引用，就能"
                "打破这个环；也可以选择环上其它任务来断开，具体在哪一处断开需要策划确认任务的"
                "先后顺序。"
            ),
            evidence=[
                Evidence(kind="graph_cycle", data={"cycle": refs}),
                # 环不属于任何单一文件，但"在哪一处断开"是有具体落点的：闭合环的那条前置引用。
                # 指到它，报告就能给出文件与行号，而不是让策划自己在一堆任务里翻。
                Evidence(
                    kind="field_path",
                    target_ref=f"quest:{refs[-1]}",
                    path=f"prerequisites.{refs[0]}",
                ),
            ],
        )


class FactionConflictRule:
    code = "FACTION_CONFLICT"
    severity = Severity.ERROR
    category = Category.GRAPH

    def check(self, ctx: AuditContext) -> Iterable[Issue]:
        member_of = _first_targets(ctx, "member_of")
        controlled_by = _first_targets(ctx, "controlled_by")
        enemies = {_pair(r.source, r.target) for r in ctx.bundle.relations if r.kind == "enemy_of"}
        for quest in ctx.bundle.quests.values():
            if _is_main_quest(quest):
                continue
            if not quest.giver_npc or not quest.location:
                continue
            npc_faction = member_of.get(quest.giver_npc)
            location_faction = controlled_by.get(quest.location)
            if npc_faction and location_faction and _pair(npc_faction, location_faction) in enemies:
                target_ref = f"quest:{quest.id}"
                yield Issue(
                    rule_code=self.code,
                    severity=self.severity,
                    category=self.category,
                    target_ref=target_ref,
                    message=(
                        f"任务《{quest.id}》发布任务的 '{quest.giver_npc}' 属于 '{npc_faction}' "
                        f"阵营，但任务地点《{quest.location}》被敌对阵营 '{location_faction}' "
                        "控制 —— 玩家要被派去敌对阵营的地盘接这个任务，这在设定上说不通。"
                    ),
                    # 没有自动修复器：三种改法（换地点/换发布人/承认是刻意的敌后任务）都合理，
                    # 选哪个是设计判断，只列出具体做法不代为拍板。
                    suggested_action=(
                        f"需要策划确认：是把任务《{quest.id}》的任务地点换成不与 "
                        f"'{npc_faction}' 敌对的地点，还是把任务发布人换成不属于 "
                        f"'{npc_faction}' 的 NPC；如果这就是刻意设计的敌后潜入任务，把这个任务"
                        "的标签加上 main 或 主线，跳过这条检查。"
                    ),
                    evidence=[
                        Evidence(
                            kind="relation",
                            relation=(quest.giver_npc, "member_of", npc_faction),
                        ),
                        Evidence(
                            kind="relation",
                            relation=(quest.location, "controlled_by", location_faction),
                        ),
                        Evidence(
                            kind="relation",
                            relation=(npc_faction, "enemy_of", location_faction),
                        ),
                    ],
                )


def _pair(left: str, right: str) -> tuple[str, str]:
    first, second = sorted((left, right))
    return first, second


def _first_targets(ctx: AuditContext, kind: str) -> dict[str, str]:
    targets: dict[str, str] = {}
    for relation in ctx.bundle.relations:
        if relation.kind == kind and relation.source not in targets:
            targets[relation.source] = relation.target
    return targets


def _is_main_quest(quest: object) -> bool:
    tags = {str(tag).strip().lower() for tag in getattr(quest, "tags", [])}
    metadata = getattr(quest, "metadata", {}) or {}
    metadata_values = {
        str(metadata.get(key, "")).strip().lower() for key in ("type", "quest_type", "category")
    }
    return bool({"main", "mainline", "主线"} & (tags | metadata_values))
