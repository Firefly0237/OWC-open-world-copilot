"""Review-queue decision workflow shared by the CLI and the Workbench UI.

Accepting an item is THE write path for AI-produced content: a quest draft is materialised into
the content store with `review_status=approved` while `origin=ai_draft` stays untouched, so the
provenance trail survives approval. Everything else only flips queue state.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..assist.review_queue import ReviewItem, ReviewItemType, ReviewQueue
from ..content.models import Quest, ReviewStatus
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
        quest = quest.model_copy(update={"review_status": ReviewStatus.APPROVED})
        bundle = project.content_store.load()
        bundle.quests[quest.id] = quest
        project.content_store.save(bundle)
        project.reload()
        written_ref = f"quest:{quest.id}"
    decided = queue.mark(item_id, "accepted", decided_by=operator)
    audit = run_full_audit(project, persist=True)
    return ReviewDecision(
        decision="accepted",
        item=decided,
        written_ref=written_ref,
        post_audit_open_errors=len(audit.open_errors),
    )
