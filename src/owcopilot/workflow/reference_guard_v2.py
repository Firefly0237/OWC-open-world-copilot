"""Current use permission for frozen ReferenceStore inputs, shared by production/playback."""

from __future__ import annotations

import json
from pathlib import Path

from ..inspiration.store import ReferenceStore


def check_reference_snapshot(root: str | Path, snapshots: list[dict]) -> None:
    if not snapshots:
        return
    sources = {row.id: row for row in ReferenceStore(root).list_sources()}
    for saved in snapshots:
        row = sources.get(saved["source_id"])
        if (
            row is None
            or "drafting" not in row.allowed_uses
            or row.quarantine_status == "quarantined"
        ):
            raise ValueError("参考许可已撤销或来源隔离")
        if row.text_hash != saved["source_hash"]:
            raise ValueError("参考来源版本已改变，不能与已冻结输入混用")


def bound_reference_snapshot(db, binding: dict) -> list[dict]:
    """Resolve immutable original run inputs, including bindings made before this guard."""
    if "reference_snapshot" in binding:
        return binding["reference_snapshot"]
    table = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='creative_v2_runs'"
    ).fetchone()
    if table is None:
        return []  # Imported non-V2 content has no attested V2 generation input.
    row = db.execute(
        "SELECT v.payload FROM artifacts AS a JOIN creative_v2_runs AS v "
        "ON v.run_id=json_extract(a.payload,'$.run_id') WHERE a.id=?",
        (binding["artifact_id"],),
    ).fetchone()
    return json.loads(row[0]).get("references", []) if row else []
