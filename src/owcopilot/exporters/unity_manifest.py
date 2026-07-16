# Unity 导入清单：com.owcopilot.content-import UPM 导入器与人工核对用的旁挂清单。
"""Unity import manifest — the sidecar the ``com.owcopilot.content-import`` UPM package reads.

The Unity importer consumes the SAME engine-agnostic ``content_bundle.json`` (dict-keyed,
snake_case) that GENERIC already writes — there is no second bundle. What the Unity side wants is
a small, checksummed manifest under ``unity/`` that (a) points its two-pass importer at the
bundle by a package-stable relative path and (b) carries the per-category content counts a designer
reconciles against the in-editor ImportReport summary ("导入完成：实体 N、任务 M…").

The counts deliberately mirror the categories the Unity importer materializes as ScriptableObject
assets (entities/quests/dialogues/dialogue_trees/relations), so the manifest is a line-by-line
cross-check target for the import report, not just provenance. Deterministic and LLM-free (offline,
$0), same as the rest of the export path.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..content.hash import content_hash
from ..content.models import ContentBundle
from .models import ExportedFile
from .unity_localization import UnityLocalizationSummary

UNITY_MANIFEST_FILENAME = "unity_import_manifest.json"
UNITY_MANIFEST_KIND = "unity_import_manifest"
UNITY_MANIFEST_SCHEMA_VERSION = 1
# The generic bundle sits at the export root; this manifest sits one level down in ``unity/``.
_BUNDLE_RELATIVE_PATH = "../content_bundle.json"


def write_unity_manifest(
    bundle: ContentBundle,
    unity_dir: str | Path,
    *,
    localization: UnityLocalizationSummary | None = None,
) -> ExportedFile:
    """Write ``unity_import_manifest.json`` into ``unity_dir`` and return its manifest entry.

    The returned ``ExportedFile.path`` is the bare filename; the caller prefixes the ``unity/``
    subdir (mirroring how the UNREAL projection's files are recorded), so the export manifest stays
    the single place that owns the subdir layout.

    ``localization`` is threaded in only when the opt-in String Table Collection CSV was written;
    it adds a ``localization`` cross-check block (collection name, CSV path, locales, string
    count) that mirrors ``content_counts`` for the localization import. When ``None`` (opt-in off)
    the payload is byte-identical to before — no new key."""
    output = Path(unity_dir)
    output.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {
        "schema_version": UNITY_MANIFEST_SCHEMA_VERSION,
        "content_hash": content_hash(bundle),
        "bundle_path": _BUNDLE_RELATIVE_PATH,
        "content_counts": {
            "entities": len(bundle.entities),
            "quests": len(bundle.quests),
            "dialogues": len(bundle.dialogues),
            "dialogue_trees": len(bundle.dialogue_trees),
            "relations": len(bundle.relations),
        },
    }
    if localization is not None:
        payload["localization"] = {
            "collection_name": localization.collection_name,
            "csv_path": localization.csv_path,
            "locales": localization.locales,
            "string_count": localization.string_count,
        }
    # Same deterministic shape as the generic bundle (sorted keys, indent 2, trailing newline).
    (output / UNITY_MANIFEST_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    digest = hashlib.sha256((output / UNITY_MANIFEST_FILENAME).read_bytes()).hexdigest()
    return ExportedFile(path=UNITY_MANIFEST_FILENAME, kind=UNITY_MANIFEST_KIND, sha256=digest)
