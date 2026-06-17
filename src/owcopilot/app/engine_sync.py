"""WS-K · engine back-sync: pull quest changes made on the engine side back into review.

The S14 export is one-way (canon -> engine). This closes the loop: given quest rows a UE/Unity
project (or a tool) edited and exported, coerce them to v2 Quests (reusing the adapter coercion),
diff against canon by content fingerprint, and queue the new/changed ones for human review — engine
edits never auto-land. The live-bridge pull itself is the deployment step; the diff+queue is here.
"""

from __future__ import annotations

from typing import Any

from ..adapters.base import coerce_quest
from ..collab import etag_for
from ..content.models import ContentBundle


def plan_engine_import(incoming: list[dict[str, Any]], bundle: ContentBundle) -> dict[str, Any]:
    """Classify each incoming quest vs canon: new / changed / unchanged (by content fingerprint)."""
    new: list[str] = []
    changed: list[str] = []
    unchanged: list[str] = []
    quests = {}
    for raw in incoming:
        quest = coerce_quest(raw)
        existing = bundle.quests.get(quest.id)
        if existing is None:
            new.append(quest.id)
            quests[quest.id] = quest
        elif etag_for(existing) != etag_for(quest):
            changed.append(quest.id)
            quests[quest.id] = quest
        else:
            unchanged.append(quest.id)
    return {
        "new": sorted(new),
        "changed": sorted(changed),
        "unchanged": sorted(unchanged),
        "_quests": quests,  # internal: the v2 quests to stage for review
    }


def staged_bundle(plan: dict[str, Any]) -> ContentBundle:
    """A bundle of just the new/changed quests, to route through the review queue (HITL)."""
    bundle = ContentBundle()
    for qid in plan["new"] + plan["changed"]:
        bundle.quests[qid] = plan["_quests"][qid]
    return bundle
