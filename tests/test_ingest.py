from owcopilot.worldbible.ingest import parse_worldbible_md
from owcopilot.worldbible.models import EntityType


def test_parse_worldbible_markdown():
    wb = parse_worldbible_md(
        """
## NPCs
- Aldric — Caravan master [merchant, quest_giver]
## Locations
- Northwatch — Fortified town
## Factions
- Ironhold Watch — Town guard [lawful]
## Relations
- Aldric -> Northwatch : located_in
- Aldric -> Ironhold Watch : member_of
"""
    )
    # entities parsed with slug ids
    assert wb.has("aldric")
    assert "Northwatch" in wb.names()
    # section -> type mapping works
    assert any(e.type == EntityType.FACTION for e in wb.entities.values())
    # tags parsed
    assert "merchant" in wb.entities["aldric"].tags
    # relations parsed AND endpoints resolved to existing entity ids
    assert len(wb.relations) == 2
    for r in wb.relations:
        assert r.source in wb.entities
        assert r.target in wb.entities
