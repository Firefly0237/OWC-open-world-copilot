from __future__ import annotations

import json

from owcopilot.audit.rules.import_rules import detect_import_conflicts
from owcopilot.content.importers.json import JSONImporter
from owcopilot.content.ingest import ChangeType, ingest_paths
from owcopilot.content.models import ContentBundle, Entity, EntityType
from owcopilot.content.normalize import normalize_raw_objects
from owcopilot.content.store import ContentStore


def test_detect_import_conflicts_for_same_id_different_content(tmp_path) -> None:
    existing = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Old desc",
            )
        }
    )
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps(
            [
                {
                    "kind": "entity",
                    "id": "npc_aldric",
                    "name": "Aldric",
                    "type": "npc",
                    "description": "New desc",
                }
            ]
        ),
        encoding="utf-8",
    )
    incoming = normalize_raw_objects(JSONImporter().parse(source))

    issues = detect_import_conflicts(existing, incoming)

    assert len(issues) == 1
    assert issues[0].rule_code == "IMPORT_CONFLICT"
    assert issues[0].target_ref == "entity:npc_aldric"
    assert issues[0].fingerprint


def test_ingest_conflict_does_not_overwrite_existing_file(tmp_path) -> None:
    store = ContentStore(tmp_path / "content")
    existing = ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Old desc",
            )
        }
    )
    store.save(existing)
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps(
            [
                {
                    "kind": "entity",
                    "id": "npc_aldric",
                    "name": "Aldric",
                    "type": "npc",
                    "description": "New desc",
                }
            ]
        ),
        encoding="utf-8",
    )

    result = ingest_paths([source], store=store, dry_run=False)

    assert result.issues[0].rule_code == "IMPORT_CONFLICT"
    assert result.changes[0].change_type is ChangeType.CONFLICT
    assert store.load().entities["npc_aldric"].description == "Old desc"
