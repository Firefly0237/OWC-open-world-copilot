"""WS-H · world analytics: a deterministic productivity dashboard for the writer.

Counts, relationship density, under-developed factions, content gaps, and readiness coverage —
all computed from the bundle (+ reusing ``assess_readiness``). No model calls; golden-testable.
This is the "where is my world thin?" view (far more useful to a solo author than reviewer stats).
"""

from __future__ import annotations

from typing import Any

from ..content.models import ContentBundle, EntityType
from ..readiness import assess_readiness


def build_world_analytics(bundle: ContentBundle) -> dict[str, Any]:
    entities = list(bundle.entities.values())
    by_type: dict[str, int] = {}
    for entity in entities:
        by_type[entity.type.value] = by_type.get(entity.type.value, 0) + 1

    counts = {
        "entities": len(entities),
        "quests": len(bundle.quests),
        "dialogues": len(bundle.dialogues),
        "dialogue_trees": len(bundle.dialogue_trees),
        "regions": len(bundle.regions),
        "pois": len(bundle.pois),
        "terms": len(bundle.terms),
        "relations": len(bundle.relations),
        "localized_texts": len(bundle.localized_texts),
    }
    relation_density = round(len(bundle.relations) / len(entities), 2) if entities else 0.0

    # factions and their member counts (member_of edges pointing at a faction)
    faction_ids = {e.id: e.name for e in entities if e.type is EntityType.FACTION}
    members: dict[str, int] = {fid: 0 for fid in faction_ids}
    for relation in bundle.relations:
        if relation.kind == "member_of" and relation.target in members:
            members[relation.target] += 1
    factions: list[dict[str, Any]] = [
        {"id": fid, "name": name, "members": members[fid]} for fid, name in faction_ids.items()
    ]
    factions.sort(key=lambda f: (int(f["members"]), str(f["id"])))
    underdeveloped = [f for f in factions if f["members"] == 0]

    # content gaps (where is it thin?)
    gaps = {
        "entities_without_description": sorted(e.id for e in entities if not e.description.strip()),
        "quests_without_objective": sorted(
            q.id for q in bundle.quests.values() if not q.objective.strip()
        ),
        "quests_without_stages": sorted(q.id for q in bundle.quests.values() if not q.stages),
    }

    report = assess_readiness(bundle)
    coverage = [{"kind": s.kind, "ready": s.ready, "total": s.total} for s in report.by_kind]

    return {
        "counts": counts,
        "entities_by_type": by_type,
        "relation_density": relation_density,
        "factions": factions,
        "underdeveloped_factions": underdeveloped,
        "gaps": gaps,
        "coverage": coverage,
        "overall_ready": report.ready_items,
        "overall_total": report.total_items,
    }
