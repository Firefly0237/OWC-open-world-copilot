"""Repair strategy: turn validation issues back into a fixed artifact.

Two strategies, same `repair(artifact, issues) -> artifact` interface:

  - `RepairStrategy`    : DETERMINISTIC remap of unknown refs to a valid same-type entity.
                          Zero-token, always terminates — the reliable fallback.
  - `LLMRepairStrategy` : asks the model for a *localised* fix (cheaper than regenerating
                          the whole artifact), validates the result, and falls back to the
                          deterministic strategy if the LLM's fix still fails.

Every model call goes through `LLMGateway.complete` (project guardrail #1).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from typing import Any

from ..core.state import ValidationIssue
from ..generation.quest import parse_quest
from ..llm.gateway import LLMGateway
from ..worldbible.models import EntityType, WorldBible

Validator = Callable[[dict[str, Any]], list[ValidationIssue]]


class RepairStrategy:
    """Deterministic remap: point a bad reference at a valid, lore-safe entity.

    Handles the structurally-fixable issue codes:
      - UNKNOWN_NPC / UNKNOWN_LOCATION -> first valid same-type entity.
      - FACTION_CONFLICT               -> a location the giver's faction holds (or at least
                                          one no enemy faction controls).
    Zero-token and always terminating, so it is the reliable fallback under LLMRepairStrategy.
    """

    def __init__(self, wb: WorldBible):
        self.wb = wb

    def repair(self, artifact: dict[str, Any], issues: list[ValidationIssue]) -> dict[str, Any]:
        fixed = dict(artifact)
        locations = [e.name for e in self.wb.by_type(EntityType.LOCATION)]
        npcs = [e.name for e in self.wb.by_type(EntityType.NPC)]
        for i in issues:
            if i.code == "UNKNOWN_LOCATION" and locations:
                fixed["location"] = locations[0]
            elif i.code == "UNKNOWN_NPC" and npcs:
                fixed["giver_npc"] = npcs[0]
            elif i.code == "FACTION_CONFLICT":
                safe = self._friendly_location(fixed.get("giver_npc"))
                if safe:
                    fixed["location"] = safe
        return fixed

    def _friendly_location(self, npc_name: str | None) -> str | None:
        """A location the NPC's faction controls (preferred), else one no enemy faction holds."""
        if not npc_name:
            return None
        name_to_id = {e.name: e.id for e in self.wb.entities.values()}
        npc_faction = self._first_target(name_to_id.get(npc_name), "member_of")
        enemies = self._enemies_of(npc_faction)
        own, neutral = [], []
        for loc in self.wb.by_type(EntityType.LOCATION):
            ctrl = self._first_target(loc.id, "controlled_by")
            if npc_faction is not None and ctrl == npc_faction:
                own.append(loc.name)
            elif ctrl not in enemies:
                neutral.append(loc.name)
        return own[0] if own else (neutral[0] if neutral else None)

    def _first_target(self, source_id: str | None, kind: str) -> str | None:
        if source_id is None:
            return None
        for r in self.wb.relations:
            if r.source == source_id and r.kind == kind:
                return r.target
        return None

    def _enemies_of(self, faction_id: str | None) -> set[str]:
        if faction_id is None:
            return set()
        out: set[str] = set()
        for r in self.wb.relations:
            if r.kind == "enemy_of":
                if r.source == faction_id:
                    out.add(r.target)
                elif r.target == faction_id:
                    out.add(r.source)
        return out


class LLMRepairStrategy:
    """Localised LLM fix with a deterministic fallback.

    Feeds the offending artifact + the issues + the repair rules to the model and parses a
    corrected Quest (structured output). The fix is re-validated here so the strategy can
    guarantee its return value is the best available:
      1. LLM fix parses AND passes -> return it.
      2. LLM fix parses but still has errors -> let the deterministic strategy finish it.
      3. LLM fix unparseable -> deterministic strategy on the original artifact.

    `validators` are the same checks the orchestrator runs, so the self-check matches the
    loop's definition of "consistent".
    """

    def __init__(
        self,
        gateway: LLMGateway,
        wb: WorldBible,
        *,
        validators: Sequence[Validator] = (),
        fallback: RepairStrategy | None = None,
    ):
        self.gateway = gateway
        self.wb = wb
        self.validators = list(validators)
        self.fallback = fallback if fallback is not None else RepairStrategy(wb)

    def repair(self, artifact: dict[str, Any], issues: list[ValidationIssue]) -> dict[str, Any]:
        fixed = self._llm_fix(artifact, issues)
        if fixed is None:  # case 3: unparseable
            return self.fallback.repair(artifact, issues)
        remaining = self._errors(fixed)
        if not remaining:  # case 1: clean
            return fixed
        return self.fallback.repair(fixed, remaining)  # case 2: partial -> finish deterministically

    def _llm_fix(
        self, artifact: dict[str, Any], issues: list[ValidationIssue]
    ) -> dict[str, Any] | None:
        system = self._system_prompt(issues)
        user = self._user_prompt(artifact)
        raw = self.gateway.complete(task="repair", system=system, user=user)
        try:
            return parse_quest(raw).model_dump(exclude_none=True)
        except Exception:
            return None

    def _system_prompt(self, issues: list[ValidationIssue]) -> str:
        npcs = ", ".join(e.name for e in self.wb.by_type(EntityType.NPC)) or "(none)"
        locs = ", ".join(e.name for e in self.wb.by_type(EntityType.LOCATION)) or "(none)"
        problems = "\n".join(f"- [{i.code}] {i.message}" for i in issues) or "- (none)"
        # "TASK: REPAIR" is a stable marker offline fakes can key on to return corrected JSON.
        return (
            "TASK: REPAIR a game quest so it satisfies the World Bible. Change as little as "
            "possible — fix only the listed problems and keep every other field intact.\n"
            "Return ONE JSON object with keys: "
            "title, giver_npc, location, objective, reward, prerequisites, timeline_order.\n"
            "Only reference entities that exist in the World Bible.\n\n"
            f"Valid NPCs: {npcs}\nValid locations: {locs}\n\n"
            f"Problems to fix:\n{problems}"
        )

    @staticmethod
    def _user_prompt(artifact: dict[str, Any]) -> str:
        return json.dumps(artifact)

    def _errors(self, artifact: dict[str, Any]) -> list[ValidationIssue]:
        out: list[ValidationIssue] = []
        for v in self.validators:
            out.extend(i for i in v(artifact) if i.severity == "error")
        return out
