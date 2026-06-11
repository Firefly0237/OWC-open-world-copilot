from __future__ import annotations

import json

from owcopilot.content.ingest import ChangeType, ingest_paths
from owcopilot.content.store import ContentStore


def test_ingest_dry_run_reports_adds_without_writing(tmp_path) -> None:
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps(
            [
                {
                    "kind": "entity",
                    "id": "npc_aldric",
                    "name": "Aldric",
                    "type": "npc",
                }
            ]
        ),
        encoding="utf-8",
    )
    store = ContentStore(tmp_path / "content")

    result = ingest_paths([source], store=store)

    assert result.dry_run is True
    assert result.incoming_count == 1
    assert result.changes[0].change_type is ChangeType.ADD
    assert not (tmp_path / "content" / "world" / "entities" / "npc_aldric.json").exists()


def test_ingest_commit_writes_when_there_are_no_errors(tmp_path) -> None:
    source = tmp_path / "entities.csv"
    source.write_text("kind,id,name,type\nentity,npc_mara,Mara,npc\n", encoding="utf-8")
    store = ContentStore(tmp_path / "content")

    result = ingest_paths([source], store=store, dry_run=False)

    assert result.issues == []
    assert (tmp_path / "content" / "world" / "entities" / "npc_mara.json").exists()
    assert store.load().entities["npc_mara"].name == "Mara"
