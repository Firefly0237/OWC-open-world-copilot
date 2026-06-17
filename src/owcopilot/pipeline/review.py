"""Review-queue decision workflow shared by the CLI and the Workbench UI.

Accepting an item is THE write path for AI-produced content: a quest draft is materialised into
the content store with `review_status=approved` while `origin=ai_draft` stays untouched, so the
provenance trail survives approval. Everything else only flips queue state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ..assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
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
        project.content_store.save(bundle)
        project.reload()
        written_ref = f"quest:{quest.id}"
    elif item.item_type is ReviewItemType.QUEST_LOGIC_DRAFT:
        # B7: apply ONLY the drafted logic layer to an existing quest (everything else untouched).
        from ..content.models import QuestLogic

        quest_id = str(item.payload.get("quest_id") or "")
        bundle = project.content_store.load()
        if quest_id not in bundle.quests:
            raise ValueError(f"任务「{quest_id}」已不存在，无法应用逻辑草稿；请驳回此项。")
        logic = QuestLogic.model_validate(item.payload.get("logic") or {})
        bundle.quests[quest_id] = bundle.quests[quest_id].model_copy(update={"logic": logic})
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
