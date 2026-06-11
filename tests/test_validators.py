"""P1 consistency-hub validators: prerequisite cycles, faction conflicts, timeline order.

Each validator gets a positive (caught) and a negative (passes) case. See
tests/test_worldbible.py for the cycle-detection primitive these build on.
"""

from owcopilot.consistency.validators import (
    FactionConflictValidator,
    PrerequisiteCycleValidator,
    ReferenceValidator,
    TimelineValidator,
)
from owcopilot.worldbible.graph import LoreGraph
from owcopilot.worldbible.models import Entity, EntityType, Relation, WorldBible


# --------------------------------------------------------------- PrerequisiteCycleValidator
def _quest_world(*ids: str) -> WorldBible:
    wb = WorldBible()
    for i in ids:
        wb.add_entity(Entity(id=i, name=i, type=EntityType.EVENT))
    return wb


def test_prereq_cycle_is_caught():
    wb = _quest_world("q1", "q2", "q3")
    # q1 -> q2 -> q3 -> q1 : a prerequisite loop that makes the region uncompletable
    wb.add_relation(Relation(source="q1", target="q2", kind="requires"))
    wb.add_relation(Relation(source="q2", target="q3", kind="requires"))
    wb.add_relation(Relation(source="q3", target="q1", kind="requires"))
    issues = PrerequisiteCycleValidator(LoreGraph(wb))({})
    assert [i.code for i in issues] == ["PREREQ_CYCLE"]
    assert issues[0].severity == "error"


def test_prereq_acyclic_passes():
    wb = _quest_world("q1", "q2", "q3")
    wb.add_relation(Relation(source="q1", target="q2", kind="requires"))
    wb.add_relation(Relation(source="q2", target="q3", kind="requires"))
    assert PrerequisiteCycleValidator(LoreGraph(wb))({}) == []


def test_prereq_cycle_created_by_current_artifact_is_caught():
    wb = _quest_world("q1", "q2")
    wb.add_relation(Relation(source="q2", target="q1", kind="requires"))
    issues = PrerequisiteCycleValidator(LoreGraph(wb))({"title": "q1", "prerequisites": ["q2"]})
    assert [i.code for i in issues] == ["PREREQ_CYCLE"]


# --------------------------------------------------------------- ReferenceValidator
def test_reference_validator_checks_entity_type_not_just_name_existence():
    wb = WorldBible()
    wb.add_entity(Entity(id="loc_aldric", name="Aldric", type=EntityType.LOCATION))
    wb.add_entity(Entity(id="npc_mira", name="Mira", type=EntityType.NPC))
    issues = ReferenceValidator(wb)({"giver_npc": "Aldric", "location": "Aldric"})
    assert [i.code for i in issues] == ["UNKNOWN_NPC"]


# --------------------------------------------------------------- FactionConflictValidator
def _faction_world() -> WorldBible:
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
    return wb


def test_faction_conflict_is_flagged():
    # Aldric (Ironhold Watch) sent to Shadowfen (held by enemy Marsh Reavers)
    issues = FactionConflictValidator(_faction_world())(
        {"giver_npc": "Aldric", "location": "Shadowfen"}
    )
    assert [i.code for i in issues] == ["FACTION_CONFLICT"]
    assert issues[0].entity_ref == "Shadowfen"


def test_faction_compatible_passes():
    # Aldric sent to Northwatch (his own faction's seat) — no conflict
    assert (
        FactionConflictValidator(_faction_world())(
            {"giver_npc": "Aldric", "location": "Northwatch"}
        )
        == []
    )


# --------------------------------------------------------------- TimelineValidator
def _timeline_world() -> WorldBible:
    wb = WorldBible()
    wb.add_entity(
        Entity(id="e1", name="The Caravan Ambush", type=EntityType.EVENT, tags=["order=1"])
    )
    wb.add_entity(
        Entity(id="e2", name="The Healer's Plea", type=EntityType.EVENT, tags=["order=2"])
    )
    wb.add_entity(
        Entity(id="e3", name="The Siege of Northwatch", type=EntityType.EVENT, tags=["order=3"])
    )
    return wb


def test_timeline_out_of_order_is_flagged():
    # order-1 quest listing an order-3 event as a prerequisite cannot be completed in time
    issues = TimelineValidator(_timeline_world())(
        {"title": "The Caravan Ambush", "prerequisites": ["The Siege of Northwatch"]}
    )
    assert [i.code for i in issues] == ["TIMELINE_VIOLATION"]
    assert issues[0].entity_ref == "The Siege of Northwatch"


def test_timeline_in_order_passes():
    issues = TimelineValidator(_timeline_world())(
        {
            "title": "The Siege of Northwatch",
            "prerequisites": ["The Caravan Ambush", "The Healer's Plea"],
        }
    )
    assert issues == []


def test_timeline_order_field_supports_new_generated_quests():
    issues = TimelineValidator(_timeline_world())(
        {
            "title": "Brand New Quest",
            "timeline_order": 1,
            "prerequisites": ["The Siege of Northwatch"],
        }
    )
    assert [i.code for i in issues] == ["TIMELINE_VIOLATION"]
