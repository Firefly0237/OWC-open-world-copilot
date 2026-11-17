# CLI 与工作台共用的审阅队列决策工作流（接受/拒绝/改；唯一写 canon 路径的驱动）。
"""Review-queue decision workflow shared by the CLI and the Workbench UI.

Accepting an item is THE write path for AI-produced content: a quest draft is materialised into
the content store with `review_status=approved` while `origin=ai_draft` stays untouched, so the
provenance trail survives approval. Everything else only flips queue state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..collab import etag_for
from ..content.hash import content_hash
from ..content.models import (
    ContentBundle,
    DialogueTree,
    Entity,
    Origin,
    Quest,
    Relation,
    ReviewStatus,
)
from ..storage.world_lock import world_write_lock
from ..trust.security import resolve_under_root
from .audit import run_full_audit
from .project import ProjectContext


@dataclass
class ReviewDecision:
    decision: str
    item: ReviewItem
    written_ref: str | None = None
    post_audit_open_errors: int = 0


def _check_creative_scope(project: ProjectContext, item: ReviewItem) -> None:
    """Revalidate the exact studio revision under the same lock as the canon write."""
    from ..workflow.store import WorkflowStore

    scope = item.payload.get("creative_scope")
    if not isinstance(scope, dict) or item.item_type is not ReviewItemType.SYNC_DRAFT:
        raise ValueError("创作草稿来源范围无效")
    root = project.content_store.root.resolve()
    if (
        scope.get("world_id") != project.world_id
        or scope.get("version") != project.version
        or scope.get("content_root_hash") != content_hash(str(root))
    ):
        raise ValueError("创作草稿不属于当前项目或版本，拒绝跨范围批准")
    path = resolve_under_root(root, root / ".owcopilot/creative_workflows.sqlite3")
    store = WorkflowStore(path)
    try:
        task = store.get_task(str(scope.get("task_id", "")))
    except KeyError as exc:
        raise ValueError("创作草稿的来源任务已不存在") from exc
    if (
        scope.get("project") != task.project
        or scope.get("revision") != task.revision
        or scope.get("world_id") != task.request.world_id
        or scope.get("version") != task.base_version
        or scope.get("base_hash") != task.base_hash
    ):
        raise ValueError("创作草稿的任务版本已改变，请提交当前版本")
    if content_hash(project.bundle) != task.base_hash:
        raise ValueError("正典基线已改变，创作草稿不能覆盖当前内容")
    incoming = ContentBundle.model_validate(item.payload.get("bundle") or {})
    if content_hash(incoming) != scope.get("candidate_hash"):
        raise ValueError("送审候选已改变，请重新从当前任务送审")


def decide_review_item(
    project: ProjectContext,
    item_id: str,
    *,
    decision: str,
    operator: str,
) -> ReviewDecision:
    """Serialize the complete review decision against this world's canon."""
    with world_write_lock(project.content_store.root):
        if decision == "accepted":
            # A direct CLI/service caller may have opened the ProjectContext before waiting for
            # another writer. Refresh under the lock so duplicate checks and the pre-write audit
            # see the revision that will actually be modified.
            project.reload()
        return _decide_review_item_unlocked(
            project,
            item_id,
            decision=decision,
            operator=operator,
        )


def _decide_review_item_unlocked(
    project: ProjectContext,
    item_id: str,
    *,
    decision: str,
    operator: str,
) -> ReviewDecision:
    if decision not in {"accepted", "rejected"}:
        raise ValueError(f"decision must be 'accepted' or 'rejected', got {decision!r}")
    if not operator.strip():
        raise ValueError("operator is required for review decisions")
    queue = ReviewQueue(project.sqlite_store)
    item = queue.get(item_id)
    # Decisions are final. Without this guard a second accept/reject (double click, REST
    # retry, two tabs) could flip an already-materialised item to "rejected" and corrupt
    # the provenance trail, or re-merge a bundle.
    if item.status != "pending_review":
        raise ValueError(
            f"review item '{item_id}' was already decided ({item.status}); "
            "decisions are final — generate a new draft instead"
        )
    # Canon writes go through the project's (world, version) scope, not the bare baseline: on a
    # migrated world the project runs on a working line, and a bare ``store.save`` would write the
    # working line's effective content straight into the baseline root tree (cross-version
    # back-contamination). For the default scope (``v1``) ``load_scoped`` / ``save_scoped`` are
    # byte-identical to ``load`` / ``save``, so single-world behaviour is unchanged.

    if decision == "rejected":
        decided = queue.mark(item_id, "rejected", decided_by=operator)
        return ReviewDecision(decision="rejected", item=decided)

    if item.item_type is ReviewItemType.PATCH_CANDIDATE:
        raise ValueError(
            "patch candidates are applied with the apply workflow "
            "(owcopilot apply --patch-id ...), not the review queue"
        )
    if "creative_scope" in item.payload:
        _check_creative_scope(project, item)
    written_ref: str | None = None
    if item.item_type is ReviewItemType.QUEST_DRAFT:
        quest = Quest.model_validate(item.payload)
        if quest.id in project.bundle.quests:
            raise ValueError(
                f"quest draft '{quest.id}' would overwrite existing quest content; "
                "reject it and generate a draft with a unique id"
            )
        quest = quest.model_copy(update={"review_status": ReviewStatus.APPROVED})
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        bundle.quests[quest.id] = quest
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = f"quest:{quest.id}"
    elif item.item_type is ReviewItemType.QUEST_LOGIC_DRAFT:
        # B7: apply ONLY the drafted logic layer to an existing quest (everything else untouched).
        from ..content.models import QuestLogic
        from ..logic import audit_quest_logic

        quest_id = str(item.payload.get("quest_id") or "")
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        if quest_id not in bundle.quests:
            raise ValueError(f"任务「{quest_id}」已不存在，无法应用逻辑草稿；请驳回此项。")
        logic = QuestLogic.model_validate(item.payload.get("logic") or {})
        candidate_quest = bundle.quests[quest_id].model_copy(update={"logic": logic})
        logic_issues = audit_quest_logic(candidate_quest)
        if logic_issues:
            preview = "；".join(f"{issue.code}: {issue.message}" for issue in logic_issues[:5])
            raise ValueError(
                f"逻辑草稿未通过确定性审计，不能写入正典；请继续修正或驳回。问题：{preview}"
            )
        bundle.quests[quest_id] = candidate_quest
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = f"quest:{quest_id}"
    elif item.item_type is ReviewItemType.TEMPORAL_SPEC_DRAFT:
        # Spec authoring is a two-phase HITL write: translation/submission only queues a candidate;
        # this branch is the sole point that may append it to canon. Re-read and validate against
        # the current quest under the world lock so a stale/tampered queue row fails closed.
        from ..logic.temporal import SpecStatus, TemporalSpec, check_temporal_spec

        quest_id = str(item.payload.get("quest_id") or "")
        if not quest_id or item.object_ref != f"quest:{quest_id}":
            raise ValueError("时序规约草稿缺少有效任务绑定，不能写入正典；请驳回此项。")
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        target_quest = bundle.quests.get(quest_id)
        if target_quest is None:
            raise ValueError(f"任务「{quest_id}」已不存在，无法应用时序规约；请驳回此项。")
        raw_spec = item.payload.get("spec")
        if not isinstance(raw_spec, dict):
            raise ValueError("时序规约草稿 payload.spec 不是对象，不能写入正典。")
        unknown_spec_fields = set(raw_spec) - set(TemporalSpec.model_fields)
        if unknown_spec_fields:
            raise ValueError(
                "时序规约草稿包含未声明字段，拒绝写入：" + "、".join(sorted(unknown_spec_fields))
            )
        spec = TemporalSpec.model_validate(raw_spec)
        if not spec.id.strip() or spec.id == "spec" or spec.provenance != "planner":
            raise ValueError("时序规约草稿缺少稳定 id 或策划来源标记，不能写入正典。")
        from ..spec_authoring import (
            OutcomeStatus,
            ScriptedSpecTranslator,
            SpecDraft,
            translate_intent,
        )

        queued_intent = str(item.payload.get("intent") or "").strip()
        queued_draft = SpecDraft.model_validate(item.payload.get("draft") or {})
        replayed = translate_intent(
            queued_intent,
            target_quest,
            bundle,
            ScriptedSpecTranslator(queued_draft),
        )
        if replayed.status is not OutcomeStatus.NEEDS_CONFIRMATION or replayed.spec is None:
            raise ValueError(
                "时序规约草稿已无法针对当前任务重新接地，不能写入正典；请重新翻译并送审。"
            )
        if replayed.spec.model_dump(mode="json") != spec.model_dump(mode="json"):
            raise ValueError("时序规约草稿与服务器重建结果不一致（可能已过期或被修改），拒绝写入。")
        if item.payload.get("back_translation") != replayed.back_translation:
            raise ValueError("时序规约回译与服务器重建结果不一致，拒绝写入。")
        verdict = check_temporal_spec(target_quest, spec)
        if verdict.status is SpecStatus.ERROR:
            raise ValueError(
                "时序规约已无法在当前任务上解析或求值，不能写入正典；请重新翻译并送审。"
            )

        candidate_quest = target_quest.model_copy(deep=True)
        metadata = dict(candidate_quest.metadata)
        if "temporal_specs" in metadata and not isinstance(metadata["temporal_specs"], list):
            raise ValueError(
                "任务 metadata.temporal_specs 不是列表；为避免覆盖未知数据，已拒绝写入。"
            )
        existing_specs = list(metadata.get("temporal_specs") or [])
        existing_ids = {
            str(raw.get("id"))
            for raw in existing_specs
            if isinstance(raw, dict) and raw.get("id") is not None
        }
        if spec.id in existing_ids:
            raise ValueError(f"任务「{quest_id}」已存在时序规约「{spec.id}」，拒绝重复追加。")
        existing_specs.append(spec.model_dump(mode="json"))
        metadata["temporal_specs"] = existing_specs
        bundle.quests[quest_id] = candidate_quest.model_copy(update={"metadata": metadata})
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = f"quest:{quest_id}"
    elif item.item_type is ReviewItemType.SYNC_DRAFT:
        incoming = ContentBundle.model_validate(item.payload.get("bundle") or {})
        baseline = ContentBundle.model_validate(item.payload.get("baseline_bundle") or {})
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        approved = _approve_bundle(incoming)
        conflicts = _apply_guarded_sync(bundle, approved, baseline)
        if conflicts:
            raise ValueError(
                "同步草稿入队后正典又发生了变化，已拒绝覆盖；请重新回拉并处理冲突："
                + ", ".join(conflicts[:8])
            )
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = item.object_ref
    elif item.item_type in {ReviewItemType.WORLD_SEED, ReviewItemType.IMPORT_DRAFT}:
        seed_bundle = ContentBundle.model_validate(item.payload.get("bundle") or {})
        conflicts = _world_seed_conflicts(project.bundle, seed_bundle)
        if conflicts:
            preview = ", ".join(conflicts[:8])
            raise ValueError(
                f"draft bundle would overwrite existing project content; "
                f"conflicting refs: {preview}"
            )
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        approved = _approve_bundle(seed_bundle)
        _merge_bundle(bundle, approved)
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = item.object_ref
    elif item.item_type is ReviewItemType.DIALOGUE_TREE:
        tree = DialogueTree.model_validate(item.payload)
        if tree.id in project.bundle.dialogue_trees:
            raise ValueError(
                f"dialogue tree '{tree.id}' would overwrite existing content; "
                "reject it and regenerate with a unique id"
            )
        tree = tree.model_copy(update={"review_status": ReviewStatus.APPROVED})
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        bundle.dialogue_trees[tree.id] = tree
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = f"dialogue_tree:{tree.id}"
    elif item.item_type is ReviewItemType.CHARACTER_PROFILE:
        entity = Entity.model_validate(item.payload.get("entity") or {})
        if entity.id in project.bundle.entities:
            raise ValueError(
                f"character '{entity.id}' would overwrite existing content; "
                "reject it and regenerate with a unique id"
            )
        relations = [Relation.model_validate(raw) for raw in (item.payload.get("relations") or [])]
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        bundle.entities[entity.id] = entity.model_copy(
            update={"review_status": ReviewStatus.APPROVED}
        )
        existing_keys = {(r.source, r.target, r.kind) for r in bundle.relations}
        for relation in relations:
            if (relation.source, relation.target, relation.kind) not in existing_keys:
                bundle.relations.append(relation)
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = f"entity:{entity.id}"
    elif item.item_type is ReviewItemType.FLAVOR_BATCH:
        entities = [Entity.model_validate(raw) for raw in (item.payload.get("entities") or [])]
        existing = [entity.id for entity in entities if entity.id in project.bundle.entities]
        if existing:
            raise ValueError(
                "flavor batch would overwrite existing entities: " + ", ".join(existing[:8])
            )
        bundle = project.content_store.load_scoped(
            world_id=project.world_id, version=project.version
        )
        for entity in entities:
            bundle.entities[entity.id] = entity.model_copy(
                update={"review_status": ReviewStatus.APPROVED}
            )
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save_scoped(bundle, version=project.version)
        project.reload()
        written_ref = item.object_ref
    decided = queue.mark(item_id, "accepted", decided_by=operator)
    audit = run_full_audit(project, persist=True)
    return ReviewDecision(
        decision="accepted",
        item=decided,
        written_ref=written_ref,
        post_audit_open_errors=len(audit.open_errors),
    )


def _world_seed_conflicts(existing: ContentBundle, incoming: ContentBundle) -> list[str]:
    conflicts: list[str] = []
    conflicts.extend(_conflict_refs("entity", existing.entities, incoming.entities))
    conflicts.extend(
        _conflict_refs(
            "quest_event_ref",
            existing.quest_event_refs,
            incoming.quest_event_refs,
        )
    )
    conflicts.extend(_conflict_refs("quest", existing.quests, incoming.quests))
    conflicts.extend(_conflict_refs("region", existing.regions, incoming.regions))
    conflicts.extend(_conflict_refs("poi", existing.pois, incoming.pois))
    conflicts.extend(_conflict_refs("dialogue", existing.dialogues, incoming.dialogues))
    conflicts.extend(
        _conflict_refs("dialogue_tree", existing.dialogue_trees, incoming.dialogue_trees)
    )
    conflicts.extend(
        _conflict_refs(
            "localized_text",
            existing.localized_texts,
            incoming.localized_texts,
        )
    )
    conflicts.extend(_conflict_refs("term", existing.terms, incoming.terms))
    conflicts.extend(_conflict_refs("style_guide", existing.style_guides, incoming.style_guides))
    conflicts.extend(_conflict_refs("style_rubric", existing.style_rubrics, incoming.style_rubrics))
    return conflicts


_SYNC_MAPPING_FIELDS = (
    "entities",
    "quest_event_refs",
    "quests",
    "regions",
    "pois",
    "dialogues",
    "dialogue_trees",
    "localized_texts",
    "terms",
    "style_guides",
    "style_rubrics",
)


def _sync_row_conflict(current: object | None, baseline: object | None) -> bool:
    if baseline is None:
        return current is not None
    return current is None or etag_for(current) != etag_for(baseline)


def _apply_guarded_sync(
    target: ContentBundle, incoming: ContentBundle, baseline: ContentBundle
) -> list[str]:
    """Apply a queued merge only where canon still equals its enqueue-time baseline."""
    conflicts: list[str] = []
    for field_name in _SYNC_MAPPING_FIELDS:
        target_rows = getattr(target, field_name)
        incoming_rows = getattr(incoming, field_name)
        baseline_rows = getattr(baseline, field_name)
        for object_id, incoming_row in incoming_rows.items():
            if _sync_row_conflict(
                target_rows.get(object_id),
                baseline_rows.get(object_id),
            ):
                conflicts.append(f"{field_name}:{object_id}")
                continue
            target_rows[object_id] = incoming_row

    def relation_key(relation: Relation) -> tuple[str, str, str]:
        return relation.source, relation.target, relation.kind

    target_relations = {relation_key(relation): relation for relation in target.relations}
    baseline_relations = {relation_key(relation): relation for relation in baseline.relations}
    for incoming_relation in incoming.relations:
        key = relation_key(incoming_relation)
        if _sync_row_conflict(
            target_relations.get(key),
            baseline_relations.get(key),
        ):
            conflicts.append(f"relation:{key[0]}:{key[1]}:{key[2]}")
            continue
        target_relations[key] = incoming_relation
    target.relations = list(target_relations.values())
    return sorted(conflicts)


def _assert_no_new_accept_errors(project: ProjectContext, candidate: ContentBundle) -> None:
    """Review accepts are write paths; block candidates that would add deterministic errors."""
    before = {
        issue_fingerprint(issue)
        for issue in project.audit_runner.run(AuditContext.from_bundle(project.bundle)).open_errors
    }
    after_result = project.audit_runner.run(AuditContext.from_bundle(candidate))
    introduced = [
        issue for issue in after_result.open_errors if issue_fingerprint(issue) not in before
    ]
    if introduced:
        preview = "；".join(
            f"{issue.rule_code} @ {issue.target_ref}: {issue.message}" for issue in introduced[:5]
        )
        raise ValueError(
            f"草稿未通过确定性审计，不能写入正典；请继续修正或驳回。新增错误：{preview}"
        )


def _conflict_refs(
    label: str,
    existing: Mapping[str, object],
    incoming: Mapping[str, object],
) -> list[str]:
    return [f"{label}:{object_id}" for object_id in sorted(set(existing) & set(incoming))]


def _approve_bundle(bundle: ContentBundle) -> ContentBundle:
    approved = bundle.model_copy(deep=True)
    for collection in [
        approved.entities.values(),
        approved.relations,
        approved.quest_event_refs.values(),
        approved.quests.values(),
        approved.regions.values(),
        approved.pois.values(),
        approved.dialogues.values(),
        approved.dialogue_trees.values(),
        approved.localized_texts.values(),
        approved.terms.values(),
        approved.style_guides.values(),
        approved.style_rubrics.values(),
    ]:
        for item in collection:
            if getattr(item, "origin", Origin.HUMAN) is not Origin.HUMAN:
                item.review_status = ReviewStatus.APPROVED
    return approved


def _merge_bundle(target: ContentBundle, incoming: ContentBundle) -> None:
    target.entities.update(incoming.entities)
    target.relations.extend(incoming.relations)
    target.quest_event_refs.update(incoming.quest_event_refs)
    target.quests.update(incoming.quests)
    target.regions.update(incoming.regions)
    target.pois.update(incoming.pois)
    target.dialogues.update(incoming.dialogues)
    target.dialogue_trees.update(incoming.dialogue_trees)
    target.localized_texts.update(incoming.localized_texts)
    target.terms.update(incoming.terms)
    target.style_guides.update(incoming.style_guides)
    target.style_rubrics.update(incoming.style_rubrics)


# --- IN-4: Read-only aggregated context for a review item -----------------------------------


def get_review_item_context_action(project: ProjectContext, item_id: str) -> dict:
    """Build the aggregated context dict for a single review item (GET :context endpoint).

    Returns a dict compatible with ReviewItemContextResponse. Raises KeyError when item_id
    does not exist in the store.

    This function is purely read-only: it never writes to the store or modifies content.
    """

    from ..assist.calibration import build_calibration_report
    from ..assist.review_queue import ReviewItem, ReviewItemType

    store = project.sqlite_store
    raw = store.get_review_item(item_id)
    if raw is None:
        raise KeyError(item_id)

    # get_review_item returns a dict with pre-parsed "payload" and "issue_refs" fields.
    raw_payload = raw.get("payload")
    payload: dict = raw_payload if isinstance(raw_payload, dict) else {}
    raw_refs = raw.get("issue_refs")
    issue_refs: list[str] = list(raw_refs) if isinstance(raw_refs, list) else []
    item_type_str: str = raw.get("item_type", "")

    # payload_summary
    if item_type_str == ReviewItemType.QUEST_DRAFT.value:
        stages_raw = payload.get("stages") or []
        stages = [
            {
                "id": str(s.get("id", "")),
                "description": str(s.get("summary", s.get("description", "")))[:100],
            }
            for s in stages_raw[:2]
            if isinstance(s, dict)
        ]
        payload_summary = {
            "title": payload.get("title"),
            "objective": payload.get("objective"),
            "stages": stages,
            "summary": None,
        }
    else:
        payload_summary = {
            "title": None,
            "objective": None,
            "stages": [],
            "summary": str(raw.get("object_ref", "")),
        }

    # refine_trail_last_reflection
    trail = payload.get("refine_trail") or []
    last_reflection: str | None = None
    if trail and isinstance(trail[-1], dict):
        last_reflection = trail[-1].get("reflection") or None

    # calibration_context: load all resolved items of the same type
    all_rows = store.list_review_items(status="accepted") + store.list_review_items(
        status="rejected"
    )
    same_type = [r for r in all_rows if r.get("item_type") == item_type_str]
    resolved = [
        ReviewItem(
            id=r["id"],
            item_type=ReviewItemType(r["item_type"]),
            object_ref=r.get("object_ref", ""),
            payload=(r["payload"] if isinstance(r.get("payload"), dict) else {}),
            issue_refs=(r["issue_refs"] if isinstance(r.get("issue_refs"), list) else []),
            status=r.get("status", "pending_review"),
            critic_verdict=r.get("critic_verdict"),
            critic_score=r.get("critic_score"),
            critic_primary_dim=r.get("critic_primary_dim"),
        )
        for r in same_type
    ]
    cal_report = build_calibration_report(resolved)
    by_type_matrix = cal_report.by_type.get(item_type_str)
    if by_type_matrix is not None:
        critic_pass_total = (
            by_type_matrix.critic_pass_human_accept + by_type_matrix.critic_pass_human_reject
        )
        fp_rate: float | None = (
            by_type_matrix.critic_pass_human_reject / critic_pass_total
            if critic_pass_total > 0
            else None
        )
    else:
        fp_rate = None

    sample_size = len(resolved)
    sufficient = sample_size >= 20

    return {
        "item_id": item_id,
        "item_type": item_type_str,
        "status": raw.get("status", ""),
        "payload_summary": payload_summary,
        "issue_refs": issue_refs,
        "critic_verdict": raw.get("critic_verdict"),
        "critic_score": raw.get("critic_score"),
        "refine_trail_last_reflection": last_reflection,
        "calibration_context": {
            "item_type": item_type_str,
            "false_pass_rate": fp_rate if sufficient else None,
            "sample_size": sample_size,
            "sufficient_sample": sufficient,
        },
    }
