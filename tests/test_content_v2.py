from __future__ import annotations

import json

from owcopilot.content.hash import content_hash
from owcopilot.content.importers.csv import CSVImporter
from owcopilot.content.importers.json import JSONImporter
from owcopilot.content.importers.markdown import parse_markdown
from owcopilot.content.models import ContentBundle, Entity, EntityType, Origin, ReviewStatus
from owcopilot.content.normalize import normalize_raw_objects


def test_content_bundle_carries_provenance_defaults() -> None:
    entity = Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)

    assert entity.origin is Origin.HUMAN
    assert entity.review_status is ReviewStatus.APPROVED

    bundle = ContentBundle()
    bundle.add_entity(entity)

    assert bundle.has_entity("npc_aldric")


def test_content_hash_is_stable_for_equivalent_payloads() -> None:
    left = {"b": [2, 1], "a": {"name": "Aldric"}}
    right = {"a": {"name": "Aldric"}, "b": [2, 1]}

    assert content_hash(left) == content_hash(right)


def test_markdown_importer_parses_world_bible_shape() -> None:
    raw = parse_markdown(
        """
## NPCs
- Aldric - Caravan master [merchant, quest_giver]
## Locations
- Northwatch - Fortified northern pass
## Relations
- Aldric -> Northwatch : located_in
""".strip()
    )

    bundle = normalize_raw_objects(raw)

    assert set(bundle.entities) == {"npc_aldric", "location_northwatch"}
    assert bundle.entities["npc_aldric"].source_ref is not None
    assert bundle.entities["npc_aldric"].tags == ["merchant", "quest_giver"]
    assert bundle.relations[0].source == "npc_aldric"
    assert bundle.relations[0].target == "location_northwatch"
    assert bundle.relations[0].kind == "located_in"


def test_json_importer_reads_native_object_list(tmp_path) -> None:
    path = tmp_path / "entities.json"
    path.write_text(
        json.dumps(
            [
                {
                    "kind": "entity",
                    "id": "npc_mara",
                    "name": "Mara",
                    "type": "npc",
                    "description": "Scout",
                }
            ]
        ),
        encoding="utf-8",
    )

    raw = JSONImporter().parse(path)
    bundle = normalize_raw_objects(raw)

    assert list(bundle.entities) == ["npc_mara"]
    assert bundle.entities["npc_mara"].description == "Scout"


def test_csv_importer_preserves_row_source_ref(tmp_path) -> None:
    path = tmp_path / "entities.csv"
    path.write_text(
        "kind,id,name,type,description,tags\n"
        "entity,npc_aldric,Aldric,npc,Caravan master,\"merchant,quest_giver\"\n",
        encoding="utf-8",
    )

    raw = CSVImporter().parse(path)
    bundle = normalize_raw_objects(raw)

    entity = bundle.entities["npc_aldric"]
    assert entity.tags == ["merchant", "quest_giver"]
    assert entity.source_ref is not None
    assert entity.source_ref.row == 2
