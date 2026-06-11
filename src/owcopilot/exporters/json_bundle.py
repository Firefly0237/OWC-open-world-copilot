"""Content export.

The engine-agnostic `content_bundle.json` + `manifest.json` pair is the universal handoff path:
deterministic files that importers or MCP workflows consume without a live editor. Unreal and
Unity targets additionally write engine-native files (DataTable CSV / per-quest JSON) so the
`--target-engine` flag changes the actual artifact set, not just the folder name.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..content.hash import content_hash
from ..content.models import ContentBundle
from .engines import write_localization_csv, write_ue_quests_csv, write_unity_quests
from .models import EngineTarget, ExportedFile, ExportManifest


def export_content_bundle(
    bundle: ContentBundle,
    output_dir: str | Path,
    *,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
) -> ExportManifest:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    engine = EngineTarget(target_engine)

    content_payload = bundle.model_dump(mode="json", exclude_none=True)
    content_text = _json(content_payload)
    (output / "content_bundle.json").write_text(content_text, encoding="utf-8")
    files = [_file_entry(output, "content_bundle.json", "content_bundle")]

    if engine is EngineTarget.UNREAL:
        write_ue_quests_csv(bundle, output / "quests_datatable.csv")
        files.append(_file_entry(output, "quests_datatable.csv", "ue_datatable_csv"))
        write_localization_csv(bundle, output / "localized_texts.csv")
        files.append(_file_entry(output, "localized_texts.csv", "localization_csv"))
    elif engine is EngineTarget.UNITY:
        for relative in write_unity_quests(bundle, output / "quests"):
            files.append(_file_entry(output, relative, "unity_quest_json"))
        files.append(_file_entry(output, "quests/index.json", "unity_index"))
        write_localization_csv(bundle, output / "localized_texts.csv")
        files.append(_file_entry(output, "localized_texts.csv", "localization_csv"))

    manifest = ExportManifest(
        target_engine=engine,
        content_hash=content_hash(bundle),
        files=files,
    )
    (output / "manifest.json").write_text(
        _json(manifest.model_dump(mode="json")),
        encoding="utf-8",
    )
    return manifest


def load_export_manifest(path: str | Path) -> ExportManifest:
    return ExportManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _file_entry(output: Path, relative: str, kind: str) -> ExportedFile:
    digest = hashlib.sha256((output / relative).read_bytes()).hexdigest()
    return ExportedFile(path=relative, kind=kind, sha256=digest)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
