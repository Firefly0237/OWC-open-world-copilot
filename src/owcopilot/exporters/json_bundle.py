# 内容导出：引擎无关的数据 + 本地化交接包。
"""Content export — the engine-agnostic data + localization handoff.

`content_bundle.json` + `manifest.json` is the universal handoff: deterministic, checksummed files
that any importer or MCP workflow consumes without a live editor. Localization travels alongside as
CSV + XLIFF 1.2 (the CAT/TMS interchange standard). This is intentionally NOT a per-engine code
generator: engine schemas differ project to project, so the value is clean, verifiable data + the
standard localization format — not bespoke scripts for a specific runtime.

Two opt-in, engine-agnostic planning artifacts (F1/N8) sit alongside the bundle: a multi-sheet
``planning_workbook.xlsx`` (``planning_workbook=True``, see ``xlsx_workbook.py``) and a
``planning_notes.docx`` (``planning_notes=True``, see ``docx_notes.py``). Both default off, so
every pre-existing call site's three-file output is unchanged.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from ..content.hash import content_hash
from ..content.models import ContentBundle
from .docx_notes import write_planning_notes
from .models import EngineTarget, ExportedFile, ExportManifest
from .unity_localization import UnityLocalizationSummary, write_unity_localization
from .unity_manifest import write_unity_manifest
from .unreal_datatable import AuditFindingInput, export_unreal_datatables
from .xliff import write_localization_csv, write_localization_xliff
from .xlsx_workbook import write_planning_workbook

_UNREAL_SUBDIR = "unreal"
_UNITY_SUBDIR = "unity"


def export_content_bundle(
    bundle: ContentBundle,
    output_dir: str | Path,
    *,
    target_engine: EngineTarget | str = EngineTarget.GENERIC,
    unity_localization: bool = False,
    row_struct_map: dict[str, dict[str, str]] | None = None,
    planning_workbook: bool = False,
    planning_notes: bool = False,
    review_findings: Iterable[AuditFindingInput] = (),
) -> ExportManifest:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    engine = EngineTarget(target_engine)  # GENERIC base; UNREAL/UNITY add an engine-side projection

    content_payload = bundle.model_dump(mode="json", exclude_none=True)
    (output / "content_bundle.json").write_bytes(_json(content_payload).encode("utf-8"))
    files = [_file_entry(output, "content_bundle.json", "content_bundle")]

    # Localization handoff: CSV for spreadsheet workflows + XLIFF 1.2 for CAT/TMS/agencies
    # (ui_max_len carried as the standard `maxwidth` attribute). Written whenever there is anything
    # to localize, so the data bundle is self-sufficient for a localization pass.
    if bundle.localized_texts or any(d.text and d.locale for d in bundle.dialogues.values()):
        write_localization_csv(bundle, output / "localized_texts.csv")
        write_localization_xliff(bundle, output / "localized_texts.xlf")
        files.append(_file_entry(output, "localized_texts.csv", "localization_csv"))
        files.append(_file_entry(output, "localized_texts.xlf", "localization_xliff"))

    # Planning materials (F1/N8): engine-agnostic, so written once regardless of target_engine —
    # a planner working in GENERIC, UNREAL or UNITY mode gets the same workbook/notes. Both opt-in
    # and default False, so every pre-existing call site keeps the exact same three-file output
    # (the golden-fixture byte tests are the sentinel).
    if planning_workbook:
        files.append(write_planning_workbook(bundle, output / "planning_workbook.xlsx"))
    if planning_notes:
        files.append(
            write_planning_notes(
                bundle, output / "planning_notes.docx", review_findings=review_findings
            )
        )

    # UNREAL target is a superset: keep the generic bundle above byte-for-byte, then add the UE
    # DataTable projection under unreal/. Preflight may raise UnrealExportError (guided) before any
    # UE file is written; the generic files already on disk stay valid.
    # ``row_struct_map`` (canonical field name -> the project's own export column name, keyed by
    # table) is meaningful only for the UNREAL projection; None/omit keeps the default {} so the
    # generic and unity outputs stay byte-for-byte identical to before.
    if engine is EngineTarget.UNREAL:
        ue_result = export_unreal_datatables(
            bundle, output / _UNREAL_SUBDIR, row_struct_map=row_struct_map
        )
        for item in ue_result.files:
            files.append(
                ExportedFile(
                    path=f"{_UNREAL_SUBDIR}/{item.path}", kind=item.kind, sha256=item.sha256
                )
            )

    # UNITY target is also a superset: the generic bundle above stays byte-for-byte, then add one
    # sidecar manifest under unity/ that points the UPM importer at the bundle and carries the
    # per-category counts for cross-checking the in-editor import report. The Unity Localization
    # String Table CSV is opt-in (`unity_localization`); with it off, the unity/ projection is
    # byte-for-byte what it was before.
    if engine is EngineTarget.UNITY:
        localization_files: list[ExportedFile] = []
        localization_summary: UnityLocalizationSummary | None = None
        if unity_localization:
            localization_files, localization_summary = write_unity_localization(
                bundle, output / _UNITY_SUBDIR
            )
        unity_file = write_unity_manifest(
            bundle, output / _UNITY_SUBDIR, localization=localization_summary
        )
        files.append(
            ExportedFile(
                path=f"{_UNITY_SUBDIR}/{unity_file.path}",
                kind=unity_file.kind,
                sha256=unity_file.sha256,
            )
        )
        for item in localization_files:
            files.append(
                ExportedFile(
                    path=f"{_UNITY_SUBDIR}/{item.path}", kind=item.kind, sha256=item.sha256
                )
            )

    manifest = ExportManifest(
        target_engine=engine,
        content_hash=content_hash(bundle),
        files=files,
    )
    (output / "manifest.json").write_bytes(_json(manifest.model_dump(mode="json")).encode("utf-8"))
    return manifest


def load_export_manifest(path: str | Path) -> ExportManifest:
    return ExportManifest.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _file_entry(output: Path, relative: str, kind: str) -> ExportedFile:
    digest = hashlib.sha256((output / relative).read_bytes()).hexdigest()
    return ExportedFile(path=relative, kind=kind, sha256=digest)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
