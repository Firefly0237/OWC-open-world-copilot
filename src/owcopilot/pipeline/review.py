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
from ..content.models import (
    ContentBundle,
    DialogueTree,
    Entity,
    Origin,
    Quest,
    Relation,
    ReviewStatus,
)
from .audit import run_full_audit
from .project import ProjectContext


@dataclass
class ReviewDecision:
    decision: str
    item: ReviewItem
    written_ref: str | None = None
    post_audit_open_errors: int = 0


def decide_review_item(
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

    if decision == "rejected":
        decided = queue.mark(item_id, "rejected", decided_by=operator)
        return ReviewDecision(decision="rejected", item=decided)

    if item.item_type is ReviewItemType.PATCH_CANDIDATE:
        raise ValueError(
            "patch candidates are applied with the apply workflow "
            "(owcopilot apply --patch-id ...), not the review queue"
        )
    written_ref: str | None = None
    if item.item_type is ReviewItemType.QUEST_DRAFT:
        quest = Quest.model_validate(item.payload)
        if quest.id in project.bundle.quests:
            raise ValueError(
                f"quest draft '{quest.id}' would overwrite existing quest content; "
                "reject it and generate a draft with a unique id"
            )
        quest = quest.model_copy(update={"review_status": ReviewStatus.APPROVED})
        bundle = project.content_store.load()
        bundle.quests[quest.id] = quest
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save(bundle)
        project.reload()
        written_ref = f"quest:{quest.id}"
    elif item.item_type is ReviewItemType.QUEST_LOGIC_DRAFT:
        # B7: apply ONLY the drafted logic layer to an existing quest (everything else untouched).
        from ..content.models import QuestLogic
        from ..logic import audit_quest_logic

        quest_id = str(item.payload.get("quest_id") or "")
        bundle = project.content_store.load()
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
        project.content_store.save(bundle)
        project.reload()
        written_ref = f"quest:{quest_id}"
    elif item.item_type in {ReviewItemType.WORLD_SEED, ReviewItemType.IMPORT_DRAFT}:
        seed_bundle = ContentBundle.model_validate(item.payload.get("bundle") or {})
        conflicts = _world_seed_conflicts(project.bundle, seed_bundle)
        if conflicts:
            preview = ", ".join(conflicts[:8])
            raise ValueError(
                f"draft bundle would overwrite existing project content; "
                f"conflicting refs: {preview}"
            )
        bundle = project.content_store.load()
        approved = _approve_bundle(seed_bundle)
        _merge_bundle(bundle, approved)
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save(bundle)
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
        bundle = project.content_store.load()
        bundle.dialogue_trees[tree.id] = tree
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save(bundle)
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
        bundle = project.content_store.load()
        bundle.entities[entity.id] = entity.model_copy(
            update={"review_status": ReviewStatus.APPROVED}
        )
        existing_keys = {(r.source, r.target, r.kind) for r in bundle.relations}
        for relation in relations:
            if (relation.source, relation.target, relation.kind) not in existing_keys:
                bundle.relations.append(relation)
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save(bundle)
        project.reload()
        written_ref = f"entity:{entity.id}"
    elif item.item_type is ReviewItemType.FLAVOR_BATCH:
        entities = [Entity.model_validate(raw) for raw in (item.payload.get("entities") or [])]
        existing = [entity.id for entity in entities if entity.id in project.bundle.entities]
        if existing:
            raise ValueError(
                "flavor batch would overwrite existing entities: " + ", ".join(existing[:8])
            )
        bundle = project.content_store.load()
        for entity in entities:
            bundle.entities[entity.id] = entity.model_copy(
                update={"review_status": ReviewStatus.APPROVED}
            )
        _assert_no_new_accept_errors(project, bundle)
        project.content_store.save(bundle)
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
    return conflicts


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
            critic_primary_dim=r.get("critic_primary_dim"),  # IN-B1 M2
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
