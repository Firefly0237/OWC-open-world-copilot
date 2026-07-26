# 人审队列（HITL）：列表/裁决/上下文/校准/改写/撤销经验。
# Review queue actions: list, decide, context, calibration, revise, revoke-lesson.

"""review actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...assist.calibration import build_calibration_report
from ...assist.characters import CharacterDraft, CharacterProfileService, OfflineCharacterProvider
from ...assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ...assist.drafts import QuestDraftService
from ...assist.lessons import extract_lessons_from_report
from ...assist.offline import OfflineQuestDraftProvider
from ...assist.review_queue import ReviewItemType, ReviewQueue
from ...content.models import ContentBundle, DialogueTree, Entity, Quest, Relation
from ...pipeline.review import decide_review_item
from ...pipeline.review import get_review_item_context_action as _get_review_item_context_action
from ...telemetry import llm_step, summarize_workflow
from ...worldgen import (
    OfflineWorldExpandProvider,
    OfflineWorldSeedProvider,
    WorldExpandBrief,
    WorldExpandService,
    WorldSeedBrief,
    WorldSeedService,
)
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway


def list_review_items_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        pending = ReviewQueue(project.sqlite_store).list_pending()
        return {
            "count": len(pending),
            "items": [item.model_dump(mode="json") for item in pending],
            "cost_budget": _deterministic_cost_budget("review_list"),
        }


def decide_review_action(
    content_root: str | Path,
    *,
    item_id: str,
    decision: str,
    operator: str,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    with _project(content_root, sqlite_path) as project:
        outcome = decide_review_item(project, item_id, decision=decision, operator=operator)
        return {
            "decision": outcome.decision,
            "item": outcome.item.model_dump(mode="json"),
            "written_ref": outcome.written_ref,
            "post_audit_open_errors": outcome.post_audit_open_errors,
            "cost_budget": _deterministic_cost_budget("review_decide"),
        }


def get_review_item_context_action(
    content_root: str | Path,
    item_id: str,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Aggregated read-only context for a single review item (IN-4).

    Raises KeyError when item_id does not exist. Never writes to the store.
    """
    with _project(content_root, sqlite_path) as project:
        return _get_review_item_context_action(project, item_id)


def reviewer_calibration_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """How well does the critic's verdict track the human's final accept/reject? Surfaces the
    false-pass blind spot (critic said pass, human rejected) over the resolved review history."""
    with _project(content_root, sqlite_path) as project:
        resolved = ReviewQueue(project.sqlite_store).list_resolved()
        report = build_calibration_report(resolved)
        # IN-3 BE-1: extract and persist lessons from false-pass data so they can be
        # injected into future drafts. This is the write trigger for the lesson archive.
        lessons_written = extract_lessons_from_report(report, project.sqlite_store)
        return {
            **report.model_dump(mode="json"),
            "lessons_written": lessons_written,
            "cost_budget": _deterministic_cost_budget("review_calibration"),
        }


def revoke_lesson_action(
    content_root: str | Path,
    *,
    lesson_id: str,
    reason: str = "",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """Soft-delete (revoke) a lesson so it stops being injected into future prompts.

    The lesson archive is upsert-only and injected automatically; a wrong or harmful lesson would
    otherwise steer every future generation/critique forever. Revocation tombstones it (records the
    reason + time) without physically deleting it. Idempotent. Raises a guided ValueError when the
    id does not exist.
    """
    with _project(content_root, sqlite_path) as project:
        revoked = project.sqlite_store.revoke_lesson(lesson_id, reason)
        if not revoked:
            raise ValueError(
                f"找不到 id 为「{lesson_id}」的教训，无法撤销。"
                "请核对教训 id（可能有误或本就不存在）后重试。"
            )
        return {
            "lesson_id": lesson_id,
            "revoked": True,
            "reason": reason,
            "cost_budget": _deterministic_cost_budget("lesson_revoke"),
        }


def revise_draft_action(
    content_root: str | Path,
    *,
    item_id: str,
    feedback: str,
    sqlite_path: str | None = None,
    budget_tokens: int = 800,
    llm_mode: str = "offline",
    llm_model: str = "deepseek-v4-flash",
) -> dict[str, Any]:
    """Feedback-driven revision: regenerate a pending draft to address a reviewer's note.

    The reviewer is no longer limited to accept/reject — they can ask for changes and the product
    revises in place (reusing each kind's regenerate-with-feedback path), and the item stays in
    review for another look. The revised draft never lands without a human still approving it."""
    note = feedback.strip()
    if not note:
        raise ValueError("feedback is required for a revision")
    with _project(content_root, sqlite_path) as project:
        queue = ReviewQueue(project.sqlite_store)
        item = queue.get(item_id)
        if item.status != "pending_review":
            raise ValueError(f"review item {item_id} is not pending (status={item.status})")

        if item.item_type is ReviewItemType.QUEST_DRAFT:
            gateway, telemetry = _gateway(
                task="quest_draft",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineQuestDraftProvider(),
            )
            revised = QuestDraftService(
                gateway=gateway,
                context_builder=project.context_builder,
                audit_runner=project.audit_runner,
                bundle=project.bundle,
            ).revise(Quest.model_validate(item.payload), note, budget_tokens=budget_tokens)
            new_payload = revised.quest.model_dump(mode="json", exclude_none=True)
            task = "quest_draft"
        elif item.item_type is ReviewItemType.CHARACTER_PROFILE:
            gateway, telemetry = _gateway(
                task="character_profile",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineCharacterProvider(),
            )
            entity = Entity.model_validate(item.payload.get("entity") or {})
            prior = CharacterDraft(
                entity=entity,
                relations=[
                    Relation.model_validate(raw) for raw in (item.payload.get("relations") or [])
                ],
                profile=dict(item.payload.get("profile") or entity.metadata.get("profile") or {}),
            )
            character = CharacterProfileService(
                gateway=gateway, bundle=project.bundle, context_builder=project.context_builder
            ).revise(prior, note, budget_tokens=budget_tokens)
            new_payload = {
                "entity": character.entity.model_dump(mode="json"),
                "relations": [r.model_dump(mode="json") for r in character.relations],
                "profile": character.profile,
                "suggested_relations": character.suggested_relations,
            }
            task = "character_profile"
        elif item.item_type is ReviewItemType.DIALOGUE_TREE:
            gateway, telemetry = _gateway(
                task="dialogue_tree",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineDialogueTreeProvider(),
            )
            tree = DialogueTreeService(gateway=gateway, bundle=project.bundle).revise(
                DialogueTree.model_validate(item.payload), note
            )
            new_payload = tree.model_dump(mode="json", exclude_none=True)
            task = "dialogue_tree"
        elif (
            item.item_type is ReviewItemType.WORLD_SEED
            and item.payload.get("kind") == "world_expand"
        ):
            # World-expansion drafts carry item_type WORLD_SEED but a different payload shape (focus
            # /angle, no seed brief). Re-grow the cohesive batch at the same focus, steered by the
            # note, via the tested staged expand pipeline — NOT the seed-revise path, which would
            # crash on the missing brief and lose the focus grounding.
            gateway, telemetry = _gateway(
                task="world_seed",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineWorldExpandProvider(),
            )
            expand_brief = WorldExpandBrief.model_validate(
                item.payload.get("brief")
                or {  # pre-brief items still carry focus_ref/angle — enough to reconstruct
                    "focus_ref": item.payload.get("focus_ref", ""),
                    "angle": item.payload.get("angle", ""),
                }
            )
            draft = WorldExpandService(
                gateway=gateway,
                bundle=project.bundle,
                project_context_builder=project.context_builder,
                reference_context_builder=project.reference_context_builder,
            ).expand(expand_brief, budget_tokens=budget_tokens, feedback=note)
            new_payload = {
                **item.payload,
                "brief": expand_brief.model_dump(mode="json"),
                "focus_label": draft.focus_label,
                "angle": draft.angle,
                "summary": f"扩写 · {draft.focus_label}",
                "bundle": draft.bundle.model_dump(mode="json", exclude_none=True),
                "grounding": draft.grounding.model_dump(mode="json"),
                "reference_report": [row.model_dump(mode="json") for row in draft.reference_report],
                "project_context_refs": draft.project_context_refs,
                "inspiration_context_refs": draft.inspiration_context_refs,
                "refine_trail": [r.model_dump(mode="json") for r in draft.refine_trail],
            }
            task = "world_seed"
        elif item.item_type is ReviewItemType.WORLD_SEED:
            gateway, telemetry = _gateway(
                task="world_seed",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineWorldSeedProvider(),
            )
            prior_bundle = ContentBundle.model_validate(item.payload.get("bundle") or {})
            seed_brief = WorldSeedBrief.model_validate(item.payload.get("brief") or {})
            draft_id = str(item.payload.get("id") or item.object_ref.split(":", 1)[-1])
            revised_bundle, revised_stage = WorldSeedService(
                gateway=gateway,
                bundle=project.bundle,
                project_context_builder=project.context_builder,
                reference_context_builder=project.reference_context_builder,
            ).revise(prior_bundle, seed_brief, note, draft_id=draft_id)
            new_payload = {
                **item.payload,
                "bundle": revised_bundle.model_dump(mode="json", exclude_none=True),
                "revised_stage": revised_stage,
            }
            task = "world_seed"
        else:
            raise ValueError(f"feedback revision is not yet supported for {item.item_type.value}")

        updated = queue.update_payload(item_id, new_payload)
        telemetry_summary = telemetry.summary()
        return {
            "item": updated.model_dump(mode="json"),
            "revised_payload": new_payload,
            "telemetry": telemetry_summary,
            "cost_budget": summarize_workflow(
                [llm_step(task, telemetry_summary)]
            ).budget.model_dump(mode="json"),
        }
