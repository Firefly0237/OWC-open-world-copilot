"""Deterministic design-readiness assessment.

Pure functions over a ``ContentBundle``: zero LLM cost, no side effects, fully reproducible —
the same backbone the audit uses, applied to completeness instead of correctness. The checklists
below are the *standard* (规范); applying them identically to every project is the *standardization*
(标准化). See ``project_docs/开发全过程.md``.
"""

from __future__ import annotations

from collections.abc import Iterable

from ..content.models import (
    POI,
    ContentBundle,
    DialogueTree,
    Entity,
    EntityType,
    Quest,
    RegionBrief,
    Term,
)
from .models import CheckResult, ItemReadiness, KindSummary, ReadinessReport

STANDARD_VERSION = "r1"

# Mirrors assist.characters.PROFILE_SECTIONS (the public character-sheet template). Kept local to
# avoid importing the LLM-facing assist module just to read a constant; both must stay in sync.
_PROFILE_SECTION_KEYS: tuple[str, ...] = (
    "appearance",
    "personality",
    "backstory",
    "motivation",
    "abilities",
    "weakness",
    "voice",
)
_MIN_OBJECTIVE_CHARS = 8


def _item(ref: str, kind: str, name: str, checks: list[CheckResult]) -> ItemReadiness:
    passed = sum(1 for c in checks if c.passed)
    score = round(passed / len(checks), 3) if checks else 1.0
    return ItemReadiness(
        ref=ref,
        kind=kind,
        name=name,
        score=score,
        ready=all(c.passed for c in checks),
        checks=checks,
        missing=[c.label for c in checks if not c.passed],
    )


def assess_quest(quest: Quest) -> ItemReadiness:
    objective = (quest.objective or "").strip()
    checks = [
        CheckResult(
            key="objective",
            label="目标描述",
            passed=len(objective) >= _MIN_OBJECTIVE_CHARS,
            detail=f"{len(objective)} 字",
        ),
        CheckResult(
            key="stages",
            label="任务阶段",
            passed=len(quest.stages) >= 1,
            detail=f"{len(quest.stages)} 个阶段",
        ),
        CheckResult(
            key="reward",
            label="奖励结构",
            passed=len(quest.rewards) >= 1,
            detail=f"{len(quest.rewards)} 项奖励",
        ),
        CheckResult(key="giver", label="发布者", passed=bool(quest.giver_npc)),
        CheckResult(key="location", label="发生地点", passed=bool(quest.location)),
    ]
    return _item(f"quest:{quest.id}", "quest", quest.title or quest.id, checks)


def assess_character(entity: Entity, *, connected_ids: set[str]) -> ItemReadiness:
    profile = entity.metadata.get("profile")
    profile = profile if isinstance(profile, dict) else {}
    missing_sections = [
        key for key in _PROFILE_SECTION_KEYS if not str(profile.get(key, "")).strip()
    ]
    checks = [
        CheckResult(
            key="description", label="简介", passed=bool((entity.description or "").strip())
        ),
        CheckResult(key="profile", label="人设档案", passed=bool(profile)),
        CheckResult(
            key="profile_complete",
            label="人设七节齐全",
            passed=not missing_sections,
            detail=("缺：" + "、".join(missing_sections)) if missing_sections else "",
        ),
        CheckResult(
            key="connected",
            label="已接入关系网",
            passed=entity.id in connected_ids,
        ),
    ]
    return _item(f"entity:{entity.id}", "character", entity.name or entity.id, checks)


def assess_region(region: RegionBrief) -> ItemReadiness:
    checks = [
        CheckResult(
            key="level_bounds",
            label="等级区间",
            passed=region.level_min is not None and region.level_max is not None,
        ),
        CheckResult(
            key="themes",
            label="主题标签",
            passed=len(region.themes) >= 1,
            detail=f"{len(region.themes)} 个主题",
        ),
    ]
    return _item(f"region:{region.id}", "region", region.name or region.id, checks)


def assess_faction(entity: Entity, *, connected_ids: set[str]) -> ItemReadiness:
    """Factions are first-class entities a level designer builds around, but they are not
    'characters' — so they get their own (lighter) completeness checklist."""
    checks = [
        CheckResult(
            key="description", label="简介", passed=bool((entity.description or "").strip())
        ),
        CheckResult(key="connected", label="已接入关系网", passed=entity.id in connected_ids),
    ]
    return _item(f"entity:{entity.id}", "faction", entity.name or entity.id, checks)


def assess_poi(poi: POI) -> ItemReadiness:
    """A point of interest is buildable level content: it needs a home region, a stated purpose,
    and a controlling faction to be designed against."""
    checks = [
        CheckResult(key="region", label="所属区域", passed=bool(poi.region_id)),
        CheckResult(
            key="purpose",
            label="功能定位",
            passed=bool((poi.purpose or "").strip()),
        ),
        CheckResult(
            key="controlling_faction",
            label="控制势力",
            passed=bool(poi.controlling_faction),
        ),
    ]
    return _item(f"poi:{poi.id}", "poi", poi.name or poi.id, checks)


def assess_term(term: Term) -> ItemReadiness:
    """A world-vocabulary term is finished when it actually explains what it means in-world."""
    checks = [
        CheckResult(
            key="description",
            label="词条释义",
            passed=bool((term.description or "").strip()),
        ),
    ]
    return _item(f"term:{term.id}", "term", term.canonical or term.id, checks)


def assess_dialogue_tree(tree: DialogueTree) -> ItemReadiness:
    has_branching = any(len(node.choices) >= 2 for node in tree.nodes.values())
    checks = [
        CheckResult(
            key="root",
            label="入口节点",
            passed=bool(tree.root_node) and tree.root_node in tree.nodes,
        ),
        CheckResult(key="nodes", label="对话节点", passed=len(tree.nodes) >= 1),
        CheckResult(key="branching", label="存在分支选项", passed=has_branching),
        CheckResult(key="participants", label="参与者", passed=len(tree.participants) >= 1),
    ]
    return _item(f"dialogue_tree:{tree.id}", "dialogue_tree", tree.title or tree.id, checks)


def _connected_entity_ids(bundle: ContentBundle) -> set[str]:
    ids: set[str] = set()
    for relation in bundle.relations:
        ids.add(relation.source)
        ids.add(relation.target)
    return ids


def assess_readiness(bundle: ContentBundle) -> ReadinessReport:
    from ..content.hash import content_hash

    connected = _connected_entity_ids(bundle)
    items: list[ItemReadiness] = []
    items.extend(assess_quest(q) for q in bundle.quests.values())
    items.extend(
        assess_character(e, connected_ids=connected)
        for e in bundle.entities.values()
        if e.type is EntityType.NPC
    )
    items.extend(
        assess_faction(e, connected_ids=connected)
        for e in bundle.entities.values()
        if e.type is EntityType.FACTION
    )
    items.extend(assess_region(r) for r in bundle.regions.values())
    items.extend(assess_poi(p) for p in bundle.pois.values())
    items.extend(assess_term(t) for t in bundle.terms.values())
    items.extend(assess_dialogue_tree(t) for t in bundle.dialogue_trees.values())

    total = len(items)
    ready = sum(1 for it in items if it.ready)
    overall = round(sum(it.score for it in items) / total, 3) if total else 1.0
    ready_rate = round(ready / total, 3) if total else 1.0

    return ReadinessReport(
        standard_version=STANDARD_VERSION,
        content_hash=content_hash(bundle),
        total_items=total,
        ready_items=ready,
        overall_score=overall,
        ready_rate=ready_rate,
        by_kind=_summarize_by_kind(items),
        items=items,
    )


def _summarize_by_kind(items: Iterable[ItemReadiness]) -> list[KindSummary]:
    buckets: dict[str, list[ItemReadiness]] = {}
    for it in items:
        buckets.setdefault(it.kind, []).append(it)
    summaries: list[KindSummary] = []
    for kind in sorted(buckets):
        group = buckets[kind]
        summaries.append(
            KindSummary(
                kind=kind,
                total=len(group),
                ready=sum(1 for it in group if it.ready),
                average_score=round(sum(it.score for it in group) / len(group), 3),
            )
        )
    return summaries
