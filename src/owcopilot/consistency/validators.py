"""Consistency validators. Prefer DETERMINISTIC checks (zero tokens, fully reliable);
reserve LLM judgement for genuinely soft questions (tone, plausibility) in P1.

Four validators make up the P1 consistency hub:
  - ReferenceValidator        : referenced NPCs / locations must exist in the World Bible.
  - PrerequisiteCycleValidator: quest-prerequisite loops make a region uncompletable.
  - FactionConflictValidator  : don't send an NPC into territory held by an enemy faction.
  - TimelineValidator         : a quest's prerequisites must occur *before* it.

Each is a `Validator` (see core/protocols.py): callable artifact -> list[ValidationIssue].
"""

from __future__ import annotations

import re
from typing import Any

import networkx as nx

from ..core.state import ValidationIssue
from ..worldbible.graph import LoreGraph
from ..worldbible.models import EntityType, WorldBible


class ReferenceValidator:
    """Every referenced NPC / location in an artifact must exist in the World Bible.

    This is the cheapest, highest-value open-world check: it stops the model from
    inventing places and people that break world coherence.
    """

    def __init__(self, wb: WorldBible):
        self.wb = wb
        self.npc_names = {e.name for e in wb.by_type(EntityType.NPC)}
        self.location_names = {e.name for e in wb.by_type(EntityType.LOCATION)}

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]:
        issues: list[ValidationIssue] = []
        npc = artifact.get("giver_npc")
        loc = artifact.get("location")
        if isinstance(npc, str) and npc and npc not in self.npc_names:
            issues.append(
                ValidationIssue(
                    code="UNKNOWN_NPC",
                    message=f"NPC '{npc}' is not a known NPC in the World Bible",
                    entity_ref=npc,
                )
            )
        if isinstance(loc, str) and loc and loc not in self.location_names:
            issues.append(
                ValidationIssue(
                    code="UNKNOWN_LOCATION",
                    message=f"Location '{loc}' is not a known location in the World Bible",
                    entity_ref=loc,
                )
            )
        return issues


class PrerequisiteCycleValidator:
    """Flag quest-prerequisite loops (A requires B requires ... requires A).

    Such a loop makes a region uncompletable: no quest in the cycle can ever be started.
    The check is purely structural and zero-token — it asks the lore graph whether the
    `requires` sub-graph has a directed cycle.

    The graph is expected to carry the prerequisites as
    `Relation(source=<quest>, target=<prereq>, kind="requires")` edges (added when the
    World Bible / artifact is ingested), so this validator simply inspects it.
    """

    def __init__(self, lore: LoreGraph):
        self.lore = lore

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]:
        if self._has_cycle(artifact):
            return [
                ValidationIssue(
                    code="PREREQ_CYCLE",
                    message="Quest prerequisites form a cycle — the region would be uncompletable",
                    severity="error",
                )
            ]
        return []

    def _has_cycle(self, artifact: dict[str, Any]) -> bool:
        g = nx.DiGraph()
        for relation in self.lore.wb.relations:
            if relation.kind != "requires":
                continue
            g.add_edge(self._name_for(relation.source), self._name_for(relation.target))

        title = artifact.get("title")
        prereqs = artifact.get("prerequisites") or []
        if isinstance(title, str) and title.strip():
            for prereq in prereqs:
                if isinstance(prereq, str) and prereq.strip():
                    g.add_edge(title, prereq)

        try:
            nx.find_cycle(g, orientation="original")
            return True
        except nx.NetworkXNoCycle:
            return False

    def _name_for(self, entity_id: str) -> str:
        entity = self.lore.wb.entities.get(entity_id)
        return entity.name if entity is not None else entity_id


class FactionConflictValidator:
    """A quest must not send an NPC into a location controlled by an enemy faction.

    Rule: find the giver NPC's faction (via a `member_of` relation) and the location's
    controlling faction (via a `controlled_by` relation); if those two factions are linked
    by `enemy_of` (in either direction), the pairing is lore-breaking -> FACTION_CONFLICT.

    Unknown references are left to ReferenceValidator; missing faction data is treated as
    "no constraint" (we only flag a conflict we can positively prove).
    """

    def __init__(self, wb: WorldBible):
        self.wb = wb

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]:
        npc = artifact.get("giver_npc")
        loc = artifact.get("location")
        if not npc or not loc:
            return []
        name_to_id = {e.name: e.id for e in self.wb.entities.values()}
        npc_id, loc_id = name_to_id.get(npc), name_to_id.get(loc)
        if npc_id is None or loc_id is None:
            return []  # unknown ref — ReferenceValidator's job

        npc_faction = self._first_target(npc_id, "member_of")
        loc_faction = self._first_target(loc_id, "controlled_by")
        if npc_faction and loc_faction and self._are_enemies(npc_faction, loc_faction):
            return [
                ValidationIssue(
                    code="FACTION_CONFLICT",
                    message=(
                        f"NPC '{npc}' ({self._name(npc_faction)}) is sent to '{loc}', "
                        f"held by enemy faction '{self._name(loc_faction)}'"
                    ),
                    entity_ref=loc,
                )
            ]
        return []

    def _first_target(self, source_id: str, kind: str) -> str | None:
        for r in self.wb.relations:
            if r.source == source_id and r.kind == kind:
                return r.target
        return None

    def _are_enemies(self, fa: str, fb: str) -> bool:
        return any(
            r.kind == "enemy_of" and {r.source, r.target} == {fa, fb} for r in self.wb.relations
        )

    def _name(self, entity_id: str) -> str:
        e = self.wb.entities.get(entity_id)
        return e.name if e else entity_id


class TimelineValidator:
    """A quest's prerequisites must occur *before* the quest itself.

    Order is read from an integer `order=<n>` tag on event/quest entities in the World
    Bible (e.g. `- The Siege — ... [order=3]`). The quest is matched to an entity by its
    title, each prerequisite by name. If a prerequisite's order is >= the quest's order it
    cannot be completed in time -> TIMELINE_VIOLATION.

    Entities with no order tag (or a title not in the Bible) are skipped — placing them on
    the timeline is impossible, and unknown references belong to ReferenceValidator.
    """

    _ORDER_RE = re.compile(r"^order\s*=\s*(-?\d+)$")

    def __init__(self, wb: WorldBible):
        self.wb = wb
        self.by_name = {e.name: e for e in self.wb.entities.values()}

    def __call__(self, artifact: dict[str, Any]) -> list[ValidationIssue]:
        prereqs = artifact.get("prerequisites") or []
        if not prereqs:
            return []
        quest_order = self._artifact_order(artifact)
        if quest_order is None:
            return []  # can't place the quest on the timeline; nothing to check

        issues: list[ValidationIssue] = []
        for p in prereqs:
            p_order = self._order(self.by_name.get(p)) if isinstance(p, str) else None
            if p_order is not None and p_order >= quest_order:
                issues.append(
                    ValidationIssue(
                        code="TIMELINE_VIOLATION",
                        message=(
                            f"Prerequisite '{p}' (order {p_order}) does not occur before "
                            f"quest '{artifact.get('title')}' (order {quest_order})"
                        ),
                        entity_ref=p,
                    )
                )
        return issues

    def _artifact_order(self, artifact: dict[str, Any]) -> int | None:
        raw = artifact.get("timeline_order")
        if isinstance(raw, bool):
            return None
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            s = raw.strip()
            if s and s.lstrip("-").isdigit():
                return int(s)

        title = artifact.get("title")
        if isinstance(title, str):
            return self._order(self.by_name.get(title))
        return None

    def _order(self, entity: Any) -> int | None:
        if entity is None:
            return None
        for t in entity.tags:
            m = self._ORDER_RE.match(t.strip())
            if m:
                return int(m.group(1))
        return None
