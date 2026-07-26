# read-merge-refill 合并：DataTable 破坏性 empty-and-fill 写法的正规配套。
"""Row-array merge keyed by ``Name`` — the counterpart to UE's destructive DataTable fill.

``unreal.DataTableFunctionLibrary.fill_data_table_from_json_string`` is documented as
"Empty and fill a Data Table from JSON string": it CLEARS the whole table and repopulates it.
A per-row upsert therefore cannot patch in place; it must read the full table, merge the touched
rows in, and re-fill the entire array. This module owns that merge as a pure, stdlib-only function
so it is unit-testable off-engine and shared by every drive path.

Semantics (upsert by ``Name``):
  * an incoming row whose ``Name`` matches an existing row REPLACES that row wholesale (a DataTable
    row is a complete struct value, not a field-level patch);
  * an incoming row whose ``Name`` is new is APPENDED;
  * an existing row NOT named by any incoming row is preserved verbatim (same object, unchanged) —
    this is the invariant that makes the destructive empty-and-fill safe.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class MergeOutcome:
    """The full row-array to re-fill plus per-row upsert accounting."""

    rows: list[dict[str, Any]]
    added: int  # incoming Names not present before
    updated: int  # incoming Names that existed and whose row content changed
    unchanged: int  # existing rows no incoming row touched (preserved verbatim)


def _require_name(row: dict[str, Any]) -> str:
    name = row.get("Name")
    if not isinstance(name, str) or not name:
        raise ValueError(f"DataTable 行缺少非空的 'Name' 键（无法按行名合并）：{row!r}")
    return name


def merge_rows_by_name(
    existing: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> MergeOutcome:
    """Upsert ``incoming`` into ``existing`` by the ``Name`` row key.

    Untouched existing rows are carried over by identity (byte-for-byte). Matched rows are replaced
    whole; brand-new rows are appended in ``incoming`` order. The result is the complete array to
    hand to ``fill_data_table_from_json_string``.
    """
    incoming_by_name: dict[str, dict[str, Any]] = {}
    for row in incoming:
        incoming_by_name[_require_name(row)] = row

    existing_names: set[str] = set()
    merged: list[dict[str, Any]] = []
    updated = 0
    unchanged = 0
    for row in existing:
        name = _require_name(row)
        existing_names.add(name)
        replacement = incoming_by_name.get(name)
        if replacement is None:
            merged.append(row)  # untouched -> preserved verbatim
            unchanged += 1
        else:
            merged.append(replacement)  # upsert = full-row replace
            if replacement != row:
                updated += 1
            # an identical re-upsert is neither an update nor an add

    added = 0
    for name, row in incoming_by_name.items():
        if name not in existing_names:
            merged.append(row)  # brand-new row appended
            added += 1

    return MergeOutcome(rows=merged, added=added, updated=updated, unchanged=unchanged)
