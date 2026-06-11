from __future__ import annotations

from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus
from owcopilot.trust import summarize_provenance, unreviewed_ai_refs


def test_summarize_provenance_counts_origins_and_review_statuses() -> None:
    bundle = ContentBundle(
        entities={
            "npc_human": Entity(id="npc_human", name="Human", type=EntityType.NPC),
            "npc_ai": Entity(
                id="npc_ai",
                name="AI",
                type=EntityType.NPC,
                origin=Origin.AI_DRAFT,
                review_status=ReviewStatus.PENDING_REVIEW,
            ),
        }
    )

    summary = summarize_provenance(bundle)

    assert summary.total == 2
    assert summary.by_origin == {"ai_draft": 1, "human": 1}
    assert summary.by_review_status == {"approved": 1, "pending_review": 1}
    assert summary.unreviewed_ai_refs == ["entity:npc_ai"]


def test_unreviewed_ai_refs_excludes_approved_ai_content() -> None:
    bundle = ContentBundle(
        entities={
            "npc_ai": Entity(
                id="npc_ai",
                name="AI",
                type=EntityType.NPC,
                origin=Origin.AI_PATCH,
                review_status=ReviewStatus.APPROVED,
            )
        }
    )

    assert unreviewed_ai_refs(bundle) == []
