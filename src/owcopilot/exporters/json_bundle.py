"""Content export — the engine-agnostic data + localization handoff.

`content_bundle.json` + `manifest.json` is the universal handoff: deterministic, checksummed files
that any importer or MCP workflow consumes without a live editor. Localization travels alongside as
CSV + XLIFF 1.2 (the CAT/TMS interchange standard). This is intentionally NOT a per-engine code
generator: engine schemas differ project to project, so the value is clean, verifiable data + the
standard localization format — not bespoke scripts for a specific runtime.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..content.hash import content_hash
from ..content.models import ContentBundle
from .models import EngineTarget, ExportedFile, ExportManifest
from .xliff import write_localization_csv, write_localization_xliff


def export_content_bundle(
    bundle: ContentBundle,
    output_dir: str | Path,
    *,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
) -> ExportManifest:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    engine = EngineTarget(target_engine)  # GENERIC is the only target; kept for manifest provenance

    content_payload = bundle.model_dump(mode="json", exclude_none=True)
    (output / "content_bundle.json").write_text(_json(content_payload), encoding="utf-8")
    files = [_file_entry(output, "content_bundle.json", "content_bundle")]

    # Localization handoff: CSV for spreadsheet workflows + XLIFF 1.2 for CAT/TMS/agencies
    # (ui_max_len carried as the standard `maxwidth` attribute). Written whenever there is anything
    # to localize, so the data bundle is self-sufficient for a localization pass.
    if bundle.localized_texts or any(d.text and d.locale for d in bundle.dialogues.values()):
        write_localization_csv(bundle, output / "localized_texts.csv")
        write_localization_xliff(bundle, output / "localized_texts.xlf")
        files.append(_file_entry(output, "localized_texts.csv", "localization_csv"))
        files.append(_file_entry(output, "localized_texts.xlf", "localization_xliff"))

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
