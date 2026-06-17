from __future__ import annotations

import json

from owcopilot.content.importers.csv import CSVImporter
from owcopilot.content.ingest import ChangeType, ingest_paths
from owcopilot.content.store import ContentStore


def test_csv_importer_reads_gb18030_chinese_export(tmp_path) -> None:
    # Excel on a Chinese Windows machine exports GB18030, not UTF-8 — forcing utf-8 would crash
    # on exactly the planner's own files.
    source = tmp_path / "roles.csv"
    source.write_bytes("kind,id,name\nentity,npc_li,李白\n".encode("gb18030"))

    rows = CSVImporter().parse(source)

    assert len(rows) == 1
    assert rows[0].data["name"] == "李白"


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
