from __future__ import annotations

import json

from owcopilot.content.models import ContentBundle
from owcopilot.content.store import ContentStore
from owcopilot.pipeline.ingest import run_ingest
from owcopilot.pipeline.project import ProjectContext


def test_run_ingest_dry_run_does_not_reload_project_bundle(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps([{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "type": "npc"}]),
        encoding="utf-8",
    )
    project = ProjectContext.open(content_root)
    try:
        result = run_ingest(project, [source])

        assert result.dry_run is True
        assert "npc_aldric" not in project.bundle.entities
    finally:
        project.close()


def test_run_ingest_commit_reloads_project_indexes(tmp_path) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps([{"kind": "entity", "id": "npc_mara", "name": "Mara", "type": "npc"}]),
        encoding="utf-8",
    )
    project = ProjectContext.open(content_root)
    try:
        result = run_ingest(project, [source], dry_run=False)

        assert result.issues == []
        assert "npc_mara" in project.bundle.entities
        assert project.context_builder.build("Mara").refs == ["entity:npc_mara"]
    finally:
        project.close()
