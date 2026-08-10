from __future__ import annotations

import hashlib
import json

import pytest

from owcopilot.content.hash import content_hash
from owcopilot.content.models import ContentBundle, Entity, EntityType, Quest
from owcopilot.exporters import EngineTarget, export_content_bundle, load_export_manifest


def _bundle() -> ContentBundle:
    return ContentBundle(
        entities={
            "npc_aldric": Entity(
                id="npc_aldric",
                name="Aldric",
                type=EntityType.NPC,
                description="Caravan master",
            )
        },
        quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_aldric")},
    )


def test_export_content_bundle_writes_manifest_and_content_file(tmp_path) -> None:
    output_dir = tmp_path / "export"

    manifest = export_content_bundle(_bundle(), output_dir, target_engine=EngineTarget.GENERIC)

    content_path = output_dir / "content_bundle.json"
    manifest_path = output_dir / "manifest.json"
    assert content_path.exists()
    assert manifest_path.exists()
    assert manifest.target_engine is EngineTarget.GENERIC
    assert manifest.content_hash == content_hash(_bundle())
    assert manifest.files[0].path == "content_bundle.json"
    assert manifest.files[0].kind == "content_bundle"

    content_payload = json.loads(content_path.read_text(encoding="utf-8"))
    assert content_payload["entities"]["npc_aldric"]["name"] == "Aldric"
    assert manifest.files[0].sha256 == hashlib.sha256(content_path.read_bytes()).hexdigest()


def test_export_manifest_round_trips_from_disk(tmp_path) -> None:
    manifest = export_content_bundle(_bundle(), tmp_path / "export", target_engine="generic")

    loaded = load_export_manifest(tmp_path / "export" / "manifest.json")

    assert loaded == manifest
    assert loaded.target_engine is EngineTarget.GENERIC


def test_export_rejects_unknown_target_engine(tmp_path) -> None:
    with pytest.raises(ValueError, match="not a valid EngineTarget"):
        export_content_bundle(_bundle(), tmp_path / "export", target_engine="godot")
