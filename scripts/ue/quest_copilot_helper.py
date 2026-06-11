"""QuestCopilot — UE5 editor-side helper (runs INSIDE Unreal's Python, not in this project's venv).

This is the small bit of UE-side code the `RemoteControlBridge` drives. It upserts / reads a row
in a Quest DataTable using only the built-in `unreal` Python API — no compiled C++ plugin, so it
is not locked to a specific engine minor version.

How to use on a machine with UE5 (see docs/P3_results.md for the full walkthrough):
  1. Enable the **Remote Control API** and **Python Editor Script** plugins; restart the editor.
  2. Create a DataTable (e.g. /Game/QuestCopilot/QuestTable) whose Row Struct has the fields
     Title, GiverNPC, Location, Objective, Reward, Prerequisites (Prerequisites = Array<String>).
  3. Expose `UpsertQuestRow` / `ReadQuestRow` to Remote Control via a tiny Editor Utility object
     (a BlueprintCallable function whose body runs the matching code below), OR call these
     functions through a UE-MCP server's "execute python" tool. Point OWCOPILOT_UE_HELPER /
     OWCOPILOT_UE_TABLE at your asset paths.

The two functions deliberately mirror the bridge contract:
    upsert_quest_row(table_path, row_name, fields) -> None
    read_quest_row(table_path, row_name)           -> dict | None
"""

from __future__ import annotations

import json

try:
    import unreal  # type: ignore
except ImportError:  # importing outside the UE editor (e.g. for a syntax check) must not explode
    unreal = None  # noqa: N816


def _load_table(table_path: str):
    if unreal is None:
        raise RuntimeError(
            "This helper must run inside the Unreal Engine editor (no `unreal` module)."
        )
    table = unreal.load_asset(table_path)
    if table is None:
        raise RuntimeError(f"DataTable not found at {table_path!r} — create it and check the path.")
    return table


def _rows_as_list(table) -> list[dict]:
    # get_data_table_as_json returns the whole table as a JSON array of row objects keyed by "Name".
    return json.loads(unreal.DataTableFunctionLibrary.get_data_table_as_json(table))


def upsert_quest_row(table_path: str, row_name: str, fields: dict) -> None:
    """Insert or replace one row (idempotent on `row_name`), then save the asset."""
    table = _load_table(table_path)
    rows = {r.get("Name"): r for r in _rows_as_list(table)}
    rows[row_name] = {"Name": row_name, **fields}
    unreal.DataTableFunctionLibrary.fill_data_table_from_json_string(
        table, json.dumps(list(rows.values()))
    )
    unreal.EditorAssetLibrary.save_loaded_asset(table)


def read_quest_row(table_path: str, row_name: str):
    """Return the row dict (without the synthetic 'Name' key) or None if it is not present."""
    table = _load_table(table_path)
    for row in _rows_as_list(table):
        if row.get("Name") == row_name:
            return {k: v for k, v in row.items() if k != "Name"}
    return None
