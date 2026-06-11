"""LLM-backed repair with a deterministic fallback (P1-2c).

The LLM path is exercised offline with fake providers (corrected JSON, garbage, and a
still-inconsistent fix) so the three branches of LLMRepairStrategy.repair are covered at $0.
"""

from owcopilot.consistency.repair import LLMRepairStrategy, RepairStrategy
from owcopilot.consistency.validators import FactionConflictValidator, ReferenceValidator
from owcopilot.core.state import ValidationIssue
from owcopilot.llm.gateway import LLMGateway, StructuredFakeProvider
from owcopilot.worldbible.models import Entity, EntityType, Relation, WorldBible


def _wb() -> WorldBible:
    wb = WorldBible()
    wb.add_entity(Entity(id="aldric", name="Aldric", type=EntityType.NPC))
    wb.add_entity(Entity(id="northwatch", name="Northwatch", type=EntityType.LOCATION))
    return wb


def _gateway(provider) -> LLMGateway:
    return LLMGateway(providers={"frontier": provider})  # 'repair' routes to frontier


BAD = {
    "title": "T",
    "giver_npc": "Aldric",
    "location": "Atlantis",  # unknown location
    "objective": "o",
    "reward": "",
    "prerequisites": [],
}
ISSUES = [ValidationIssue(code="UNKNOWN_LOCATION", message="unknown", entity_ref="Atlantis")]


class _GarbageProvider:
    """Stands in for a model that returns unparseable text."""

    def complete(self, *, system, user, model):
        return "Sorry, I can't help with that.", 1, 1


def test_llm_repair_fixes_via_model():
    wb = _wb()
    good = {**BAD, "location": "Northwatch"}
    strat = LLMRepairStrategy(
        _gateway(StructuredFakeProvider(quest=good)), wb, validators=[ReferenceValidator(wb)]
    )
    fixed = strat.repair(BAD, ISSUES)
    assert fixed["location"] == "Northwatch"
    assert ReferenceValidator(wb)(fixed) == []  # the LLM fix is consistent


def test_llm_repair_falls_back_when_unparseable():
    wb = _wb()
    strat = LLMRepairStrategy(_gateway(_GarbageProvider()), wb, validators=[ReferenceValidator(wb)])
    fixed = strat.repair(BAD, ISSUES)
    assert fixed["location"] == "Northwatch"  # deterministic fallback kicked in


def test_llm_repair_falls_back_when_fix_still_inconsistent():
    wb = _wb()
    still_bad = {**BAD, "location": "Eldorado"}  # model "fixed" it to another unknown
    strat = LLMRepairStrategy(
        _gateway(StructuredFakeProvider(quest=still_bad)), wb, validators=[ReferenceValidator(wb)]
    )
    fixed = strat.repair(BAD, ISSUES)
    assert fixed["location"] == "Northwatch"  # fallback finished the job
    assert ReferenceValidator(wb)(fixed) == []


def test_deterministic_strategy_standalone():
    wb = _wb()
    fixed = RepairStrategy(wb).repair(BAD, ISSUES)
    assert fixed["location"] == "Northwatch"


def test_deterministic_repairs_faction_conflict():
    wb = WorldBible()
    wb.add_entity(Entity(id="aldric", name="Aldric", type=EntityType.NPC))
    wb.add_entity(Entity(id="northwatch", name="Northwatch", type=EntityType.LOCATION))
    wb.add_entity(Entity(id="shadowfen", name="Shadowfen", type=EntityType.LOCATION))
    wb.add_entity(Entity(id="watch", name="Ironhold Watch", type=EntityType.FACTION))
    wb.add_entity(Entity(id="reavers", name="Marsh Reavers", type=EntityType.FACTION))
    wb.add_relation(Relation(source="aldric", target="watch", kind="member_of"))
    wb.add_relation(Relation(source="northwatch", target="watch", kind="controlled_by"))
    wb.add_relation(Relation(source="shadowfen", target="reavers", kind="controlled_by"))
    wb.add_relation(Relation(source="reavers", target="watch", kind="enemy_of"))

    bad = {
        "title": "T",
        "giver_npc": "Aldric",
        "location": "Shadowfen",
        "objective": "o",
        "reward": "",
        "prerequisites": [],
    }
    issues = [ValidationIssue(code="FACTION_CONFLICT", message="x", entity_ref="Shadowfen")]
    fixed = RepairStrategy(wb).repair(bad, issues)
    assert fixed["location"] == "Northwatch"  # relocated to a faction-friendly town
    assert FactionConflictValidator(wb)(fixed) == []
