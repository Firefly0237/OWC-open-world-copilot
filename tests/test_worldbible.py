from owcopilot.worldbible.graph import LoreGraph
from owcopilot.worldbible.models import Entity, EntityType, Relation, WorldBible, world_bible_hash


def _wb():
    wb = WorldBible()
    wb.add_entity(Entity(id="a", name="Aldric", type=EntityType.NPC))
    wb.add_entity(Entity(id="n", name="Northwatch", type=EntityType.LOCATION))
    return wb


def test_worldbible_basics():
    wb = _wb()
    assert wb.has("a")
    assert "Aldric" in wb.names()
    assert [e.name for e in wb.by_type(EntityType.LOCATION)] == ["Northwatch"]


def test_world_bible_hash_is_stable_and_content_sensitive():
    a = _wb()
    b = _wb()
    assert world_bible_hash(a) == world_bible_hash(b)

    b.add_relation(Relation(source="a", target="n", kind="located_in"))
    assert world_bible_hash(a) != world_bible_hash(b)


def test_loregraph_cycle_detection():
    wb = WorldBible()
    for i in ("q1", "q2", "q3"):
        wb.add_entity(Entity(id=i, name=i, type=EntityType.EVENT))
    # q1 -> q2 -> q3 -> q1  (a prerequisite loop)
    wb.add_relation(Relation(source="q1", target="q2", kind="requires"))
    wb.add_relation(Relation(source="q2", target="q3", kind="requires"))
    wb.add_relation(Relation(source="q3", target="q1", kind="requires"))
    assert LoreGraph(wb).has_cycle(kind="requires") is True

    wb2 = WorldBible()
    for i in ("q1", "q2"):
        wb2.add_entity(Entity(id=i, name=i, type=EntityType.EVENT))
    wb2.add_relation(Relation(source="q1", target="q2", kind="requires"))
    assert LoreGraph(wb2).has_cycle(kind="requires") is False
