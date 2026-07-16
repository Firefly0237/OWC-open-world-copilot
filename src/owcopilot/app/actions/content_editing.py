# 人工直接编辑：实体/任务/关系/对白/风格/改名/快照恢复/逻辑等写入动作。
# Human direct-edit write actions across the content graph.

"""content_editing actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

import hashlib
from collections.abc import MutableMapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...assist.calibration import critic_from_trail, primary_dim_from_trail
from ...assist.review_queue import ReviewQueue
from ...content.models import ContentBundle, DialogueNode, Entity, EntityType, Relation
from ...content.relation_kinds import is_symmetric_kind
from ...pipeline.audit import run_full_audit
from ...telemetry import llm_step, summarize_workflow
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway


def update_entity_action(
    content_root: str | Path,
    *,
    entity_id: str,
    name: str | None = None,
    description: str | None = None,
    tags: list[str] | None = None,
    metadata_updates: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Edit an entity's display fields in place. The id never changes here, so references
    in quests/relations stay valid — renames of the id itself go through the impact +
    patch workflow instead. `metadata_updates` shallow-merges (a None value deletes the
    key), which is how character-sheet sections are maintained."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        entity = bundle.entities.get(entity_id)
        if entity is None:
            raise ValueError(f"实体不存在：{entity_id}")
        update: dict[str, Any] = {}
        if name is not None and name.strip():
            update["name"] = name.strip()
        if description is not None:
            update["description"] = description.strip()
        if tags is not None:
            update["tags"] = [str(tag).strip() for tag in tags if str(tag).strip()]
        if metadata_updates:
            merged = dict(entity.metadata)
            for key, value in metadata_updates.items():
                if value is None:
                    merged.pop(key, None)
                else:
                    merged[key] = value
            update["metadata"] = merged
        if update:
            bundle.entities[entity_id] = entity.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "entity": bundle.entities[entity_id].model_dump(mode="json"),
            "changed": sorted(update),
            "cost_budget": _deterministic_cost_budget("entity_update"),
        }


def update_style_guide_action(
    content_root: str | Path,
    *,
    guide_id: str = "style_guide",
    body: str | None = None,
    rules: list[str] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """B10: edit the world's style guide (worldview body + rules) in place — the same direct
    human-edit pipeline as the archive (load→save→reload, signed human, lands immediately, no AI
    review queue). The body and rules round-trip through the full-fidelity style_guides.json."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        guide = bundle.style_guides.get(guide_id)
        if guide is None:
            raise ValueError(f"风格指南不存在：{guide_id}")
        update: dict[str, Any] = {}
        if body is not None:
            update["body"] = body.strip()
        if rules is not None:
            update["rules"] = [str(r).strip() for r in rules if str(r).strip()]
        if update:
            bundle.style_guides[guide_id] = guide.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "style_guide": bundle.style_guides[guide_id].model_dump(mode="json"),
            "changed": sorted(update),
            "cost_budget": _deterministic_cost_budget("style_guide_update"),
        }


def delete_object_action(
    content_root: str | Path,
    *,
    ref_type: str,
    object_id: str,
    cascade_relations: bool = True,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Delete one object from the world. Entities optionally cascade their relations;
    anything still referencing the deleted object surfaces in the next audit (by design —
    the UI shows an impact preview before calling this)."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        removed_relations = 0
        if ref_type == "entity":
            if object_id not in bundle.entities:
                raise ValueError(f"实体不存在：{object_id}")
            del bundle.entities[object_id]
            if cascade_relations:
                before = len(bundle.relations)
                bundle.relations = [
                    rel
                    for rel in bundle.relations
                    if rel.source != object_id and rel.target != object_id
                ]
                removed_relations = before - len(bundle.relations)
        elif ref_type == "quest":
            if object_id not in bundle.quests:
                raise ValueError(f"任务不存在：{object_id}")
            del bundle.quests[object_id]
        elif ref_type == "dialogue_tree":
            if object_id not in bundle.dialogue_trees:
                raise ValueError(f"对话树不存在：{object_id}")
            del bundle.dialogue_trees[object_id]
        else:
            raise ValueError(f"不支持删除的类型：{ref_type}")
        # ContentStore.save reconciles directories against the bundle (orphan json files
        # are removed), so no per-file unlink bookkeeping belongs here.
        project.content_store.save(bundle)
        project.reload()
        audit = run_full_audit(project, persist=True)
        return {
            "deleted_ref": f"{ref_type}:{object_id}",
            "removed_relations": removed_relations,
            "post_audit_open_errors": len(audit.open_errors),
            "cost_budget": _deterministic_cost_budget("object_delete"),
        }


_ENTITY_ID_PREFIX: dict[EntityType, str] = {
    EntityType.NPC: "npc",
    EntityType.LOCATION: "loc",
    EntityType.FACTION: "fac",
    EntityType.ITEM: "item",
    EntityType.EVENT: "ev",
    EntityType.REGION: "region",
    EntityType.ORGANIZATION: "org",
    EntityType.CONCEPT: "concept",
    EntityType.TERM: "term",
    EntityType.SKILL: "skill",
    EntityType.ACHIEVEMENT: "ach",
}


def _new_entity_id(bundle: ContentBundle, *, name: str, entity_type: EntityType) -> str:
    prefix = _ENTITY_ID_PREFIX.get(entity_type, "ent")
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:6]
    candidate = f"{prefix}_{digest}"
    counter = 2
    while candidate in bundle.entities:
        candidate = f"{prefix}_{digest}_{counter}"
        counter += 1
    return candidate


def create_entity_action(
    content_root: str | Path,
    *,
    name: str,
    entity_type: str,
    description: str = "",
    tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Create a new entity from the graph (human-authored, immediate to canon)."""
    if not name.strip():
        raise ValueError("名字不能为空")
    try:
        resolved_type = EntityType(entity_type)
    except ValueError as exc:
        raise ValueError(f"不支持的实体类型：{entity_type}") from exc
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        entity = Entity(
            id=_new_entity_id(bundle, name=name, entity_type=resolved_type),
            name=name.strip(),
            type=resolved_type,
            description=description.strip(),
            tags=[str(tag).strip() for tag in (tags or []) if str(tag).strip()],
            metadata=metadata or {},
        )
        bundle.entities[entity.id] = entity
        project.content_store.save(bundle)
        project.reload()
        return {
            "entity": entity.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("entity_create"),
        }


def update_quest_action(
    content_root: str | Path,
    *,
    quest_id: str,
    title: str | None = None,
    objective: str | None = None,
    timeline_order: int | None = None,
    set_timeline_order: bool = False,
    prerequisites: list[str] | None = None,
    giver_npc: str | None = None,
    location: str | None = None,
    metadata_updates: dict[str, Any] | None = None,
    if_match: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Edit a quest's fields in place (drag-to-reorder sets ``timeline_order``).

    ``set_timeline_order`` distinguishes "set the order (possibly to None = unsequenced)" from
    "leave it unchanged", since ``timeline_order=None`` is itself a meaningful value.
    ``if_match`` is an optimistic-concurrency etag (WS-B): a stale one means another author edited
    first, so the write is refused rather than clobbering theirs."""
    from ...collab import ConflictError, check_etag, etag_for

    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        quest = bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        try:
            check_etag(current=etag_for(quest), if_match=if_match)
        except ConflictError as exc:
            raise ValueError(str(exc)) from exc
        update: dict[str, Any] = {}
        if title is not None and title.strip():
            update["title"] = title.strip()
        if objective is not None:
            update["objective"] = objective.strip()
        if set_timeline_order:
            update["timeline_order"] = timeline_order
        if prerequisites is not None:
            update["prerequisites"] = [p for p in prerequisites if p in bundle.quests]
        if giver_npc is not None:
            update["giver_npc"] = giver_npc or None
        if location is not None:
            update["location"] = location or None
        if metadata_updates:
            update["metadata"] = _merge_metadata(quest.metadata, metadata_updates)
        if update:
            bundle.quests[quest_id] = quest.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "quest": bundle.quests[quest_id].model_dump(mode="json"),
            "changed": sorted(update),
            "etag": etag_for(bundle.quests[quest_id]),
            "cost_budget": _deterministic_cost_budget("quest_update"),
        }


# >>> WS-A logic endpoints (human-edit pipeline; logic audits surface via the normal audit) >>>
def update_quest_logic_action(
    content_root: str | Path,
    *,
    quest_id: str,
    logic: dict[str, Any] | None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Set/replace a quest's native logic layer (None clears it). Malformed expressions are rejected
    up front (clean 4xx, not a 500); the saved logic's deterministic issues are returned so the
    editor sees unreachable stages / deadlocks / undefined vars immediately."""
    from ...content.models import QuestLogic
    from ...logic import LogicSyntaxError, audit_quest_logic, parse_expr

    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        quest = bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        parsed_logic: QuestLogic | None = None
        if logic is not None:
            try:
                parsed_logic = QuestLogic.model_validate(logic)
            except ValueError as exc:
                raise ValueError(f"逻辑结构不合法：{exc}") from exc
            for source in _logic_expression_sources(parsed_logic):
                try:
                    parse_expr(source)
                except LogicSyntaxError as exc:
                    raise ValueError(f"逻辑表达式无法解析「{source}」：{exc}") from exc
        bundle.quests[quest_id] = quest.model_copy(update={"logic": parsed_logic})
        project.content_store.save(bundle)
        project.reload()
        updated = bundle.quests[quest_id]
        issues = audit_quest_logic(updated)
        return {
            "quest": updated.model_dump(mode="json"),
            "logic_issues": [{"code": i.code, "message": i.message, "ref": i.ref} for i in issues],
            "cost_budget": _deterministic_cost_budget("quest_logic_update"),
        }


def _logic_expression_sources(logic: Any) -> list[str]:
    sources = [logic.precondition, *(s.precondition for s in logic.stage_logic)]
    sources += [b.condition for b in logic.branches]
    return [s for s in sources if isinstance(s, str) and s.strip()]


def draft_quest_logic_action(
    content_root: str | Path,
    *,
    quest_id: str,
    intent: str = "",
    refine_rounds: int = 2,
    sqlite_path: str | None = None,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """B7: draft a quest's logic layer with the model, gated by the deterministic logic audit, then
    queue it for human review (HITL — it never lands automatically). The audit runs every round, so
    the reviewer sees logic already machine-checked for deadlocks / unreachable stages / undefined
    vars."""
    from ...assist.offline import OfflineLogicDraftProvider
    from ...logic.draft import LOGIC_DRAFT_TASK, draft_quest_logic

    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        gateway, telemetry = _gateway(
            task=LOGIC_DRAFT_TASK,
            llm_mode=llm_mode,
            llm_model=llm_model,
            offline_provider=OfflineLogicDraftProvider(),
        )
        result = draft_quest_logic(
            gateway=gateway, quest=quest, intent=intent, max_rounds=max(0, refine_rounds) + 1
        )
        _logic_trail_dump = [r.model_dump(mode="json") for r in result.trail]
        critic_verdict, critic_score = critic_from_trail(_logic_trail_dump)
        critic_primary_dim = primary_dim_from_trail(_logic_trail_dump)
        issue_refs = [f"{i.code}:{i.ref}" for i in result.issues]
        item = ReviewQueue(project.sqlite_store).add_quest_logic_draft(
            {
                "quest_id": quest_id,
                "quest_title": quest.title,
                "logic": result.logic.model_dump(mode="json"),
            },
            issue_refs=issue_refs,
            critic_verdict=critic_verdict,
            critic_score=critic_score,
            critic_primary_dim=critic_primary_dim,
        )
        telemetry_summary = telemetry.summary()
        return {
            "quest_id": quest_id,
            "logic": result.logic.model_dump(mode="json"),
            "logic_issues": [
                {"code": i.code, "message": i.message, "ref": i.ref} for i in result.issues
            ],
            "refine_trail": [r.model_dump(mode="json") for r in result.trail],
            "auto_review_incomplete": result.auto_review_incomplete,
            "review_item_id": item.id,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step("draft_quest_logic", telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }


def simulate_quest_action(
    content_root: str | Path,
    *,
    quest_id: str,
    choices: list[str] | None = None,
    initial_state: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-E playtest: deterministically walk a quest's logic and report the path + outcome."""
    from ...logic import simulate_quest

    with _project(content_root, sqlite_path) as project:
        quest = project.bundle.quests.get(quest_id)
        if quest is None:
            raise ValueError(f"任务不存在：{quest_id}")
        run = simulate_quest(quest, choices=choices, initial_state=initial_state)
        return {
            "quest_id": quest_id,
            "run": run.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("quest_simulate"),
        }


# >>> WS-C search + safe refactor >>>
def search_all_action(
    content_root: str | Path,
    *,
    query: str,
    kinds: list[str] | None = None,
    limit: int = 30,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Global literal search across the canon (jump-to). Deterministic, $0."""
    from ...search import search_all

    with _project(content_root, sqlite_path) as project:
        hits = search_all(project.bundle, query, kinds=set(kinds) if kinds else None, limit=limit)
        return {
            "query": query,
            "hits": [hit.model_dump(mode="json") for hit in hits],
            "cost_budget": _deterministic_cost_budget("search"),
        }


def plan_rename_action(
    content_root: str | Path,
    *,
    ref: str,
    new_name: str | None = None,
    new_id: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Dry-run a rename: the edits it would make + any conflicts. Mutates nothing."""
    from ...content.refactor import plan_rename

    with _project(content_root, sqlite_path) as project:
        plan = plan_rename(project.bundle, ref=ref, new_name=new_name, new_id=new_id)
        return {
            "plan": plan.model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("rename_plan"),
        }


def apply_rename_action(
    content_root: str | Path,
    *,
    ref: str,
    operator: str,
    new_name: str | None = None,
    new_id: str | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Apply a rename atomically: snapshot (undo point) -> rewrite every reference -> save -> reload
    -> re-audit (must stay clean). Returns the snapshot id so the caller can undo."""
    from ...content.refactor import apply_rename, plan_rename
    from ...content.snapshot import write_snapshot

    if not operator.strip():
        raise ValueError("请先填写署名")
    with _project(content_root, sqlite_path) as project:
        plan = plan_rename(project.bundle, ref=ref, new_name=new_name, new_id=new_id)
        if plan.conflicts:
            raise ValueError("；".join(plan.conflicts))
        snapshot = write_snapshot(project.content_store, label=f"rename {ref} by {operator}")
        renamed = apply_rename(project.bundle, plan)
        project.content_store.save(renamed)
        project.reload()
        audit = run_full_audit(project, persist=True)
        return {
            "plan": plan.model_dump(mode="json"),
            "undo_snapshot_id": snapshot.id,
            "post_audit_open_errors": len(audit.open_errors),
            "cost_budget": _deterministic_cost_budget("rename_apply"),
        }


def restore_snapshot_action(
    content_root: str | Path,
    *,
    snapshot_id: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Undo by restoring a snapshot (e.g. the undo point a rename returned)."""
    from ...content.snapshot import load_snapshot

    with _project(content_root, sqlite_path) as project:
        bundle = load_snapshot(project.content_store, snapshot_id)
        if bundle is None:
            raise ValueError(f"快照不存在：{snapshot_id}")
        project.content_store.save(bundle)
        project.reload()
        return {
            "restored": snapshot_id,
            "cost_budget": _deterministic_cost_budget("snapshot_restore"),
        }


def add_relation_action(
    content_root: str | Path,
    *,
    source: str,
    target: str,
    kind: str,
    symmetric: bool | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Add a typed relation between two objects (dedup; symmetric kinds are peer/undirected)."""
    kind = kind.strip()
    if not kind:
        raise ValueError("关系类型不能为空")
    if source == target:
        raise ValueError("不能给同一个对象连自身")
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        if not _object_exists(bundle, source) or not _object_exists(bundle, target):
            raise ValueError("关系两端必须都是已存在的对象")
        exists = any(
            r.source == source and r.target == target and r.kind == kind for r in bundle.relations
        )
        if not exists:
            is_sym = is_symmetric_kind(kind) if symmetric is None else symmetric
            metadata = {"symmetric": True} if is_sym else {}
            bundle.relations.append(
                Relation(source=source, target=target, kind=kind, metadata=metadata)
            )
            project.content_store.save(bundle)
            project.reload()
        return {
            "relation": {"source": source, "target": target, "kind": kind},
            "added": not exists,
            "cost_budget": _deterministic_cost_budget("relation_add"),
        }


def remove_relation_action(
    content_root: str | Path,
    *,
    source: str,
    target: str,
    kind: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Remove a typed relation (exact source/kind/target match)."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        before = len(bundle.relations)
        bundle.relations = [
            r
            for r in bundle.relations
            if not (r.source == source and r.target == target and r.kind == kind)
        ]
        removed = before - len(bundle.relations)
        if removed:
            project.content_store.save(bundle)
            project.reload()
        return {"removed": removed, "cost_budget": _deterministic_cost_budget("relation_remove")}


def update_dialogue_tree_action(
    content_root: str | Path,
    *,
    tree_id: str,
    title: str | None = None,
    root_node: str | None = None,
    nodes: dict[str, Any] | None = None,
    metadata_updates: dict[str, Any] | None = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Replace a dialogue tree's editable fields (nodes map / root / title), validated and saved."""
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        tree = bundle.dialogue_trees.get(tree_id)
        if tree is None:
            raise ValueError(f"对话树不存在：{tree_id}")
        update: dict[str, Any] = {}
        if title is not None:
            update["title"] = title.strip()
        if nodes is not None:
            try:
                parsed = {nid: DialogueNode.model_validate(node) for nid, node in nodes.items()}
            except ValidationError as exc:
                # malformed node shape from the editor is bad input, not a server fault → clean 400
                raise ValueError(f"对话节点格式不合法：{exc.error_count()} 处问题") from exc
            update["nodes"] = parsed
        new_nodes = update.get("nodes", tree.nodes)
        if root_node is not None:
            update["root_node"] = root_node
        # Validate the EFFECTIVE root against the (possibly replaced) node map — not only a root the
        # caller passed this time. Replacing `nodes` while dropping/renaming the existing root id
        # must not silently persist a tree whose root points at a node that no longer exists.
        effective_root = root_node if root_node is not None else tree.root_node
        if effective_root and new_nodes and effective_root not in new_nodes:
            raise ValueError(f"根节点不存在：{effective_root}")
        if metadata_updates:
            update["metadata"] = _merge_metadata(tree.metadata, metadata_updates)
        if update:
            bundle.dialogue_trees[tree_id] = tree.model_copy(update=update)
            project.content_store.save(bundle)
            project.reload()
        return {
            "tree": bundle.dialogue_trees[tree_id].model_dump(mode="json"),
            "cost_budget": _deterministic_cost_budget("dialogue_tree_update"),
        }


def set_object_position_action(
    content_root: str | Path,
    *,
    ref: str,
    x: float,
    y: float,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Persist a dragged node's position into the object's ``metadata.graph_pos`` (the layout uses
    the override when present, else falls back to the deterministic layout)."""
    object_type, _, object_id = ref.partition(":")
    pos = [round(float(x), 1), round(float(y), 1)]
    with _project(content_root, sqlite_path) as project:
        bundle = project.content_store.load()
        collections: dict[str, MutableMapping[str, Any]] = {
            "entity": bundle.entities,
            "quest": bundle.quests,
            "poi": bundle.pois,
            "region": bundle.regions,
        }
        collection = collections.get(object_type)
        if collection is None or object_id not in collection:
            raise ValueError(f"对象不存在：{ref}")
        obj = collection[object_id]
        collection[object_id] = obj.model_copy(
            update={"metadata": {**obj.metadata, "graph_pos": pos}}
        )
        project.content_store.save(bundle)
        project.reload()
        return {
            "ref": ref,
            "graph_pos": pos,
            "cost_budget": _deterministic_cost_budget("node_move"),
        }


def _object_exists(bundle: ContentBundle, object_id: str) -> bool:
    return (
        object_id in bundle.entities
        or object_id in bundle.quests
        or object_id in bundle.pois
        or object_id in bundle.regions
        # terms are first-class graph nodes too (the index adds them, the audit treats them as
        # objects), so a relation to/from a term is legitimate and must not be rejected as unknown.
        or object_id in bundle.terms
    )


def _merge_metadata(current: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(current)
    for key, value in updates.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = value
    return merged
