"""Unreal Engine 5 adapter — the **translation layer** (pure, offline-testable).

P3 splits "land a Quest into UE5" into two halves so 90% of the work (and all the tests) stay
engine-free:
  * THIS file — translate a Quest dict into an engine-neutral DataTable command
    (`upsert_datatable_row(table, row_name, fields)`) and read it back for `snapshot()`. No UE
    dependency; fully deterministic.
  * `bridge.py` — the thin, injectable I/O layer that performs the command (FakeUnrealBridge
    offline, RemoteControlBridge against a real editor).

The orchestrator's EXECUTE node already calls `adapter.apply(artifact)`; P3 turns this stub into
a real translation without touching `core/`. The constructor keeps `bridge`/`table` optional so
existing `UnrealAdapter()` call sites (P0/P1/P2 demos, benchmark) keep working — they just land
into an in-memory FakeUnrealBridge.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from ..base import BaseEngineAdapter
from .bridge import FakeUnrealBridge, UnrealBridge

# Quest key  ->  UE Row Struct field name (FQuestTableRow). Keep aligned with the UE-side struct.
_FIELD_MAP: list[tuple[str, str]] = [
    ("title", "Title"),
    ("giver_npc", "GiverNPC"),
    ("location", "Location"),
    ("objective", "Objective"),
    ("reward", "Reward"),
    ("prerequisites", "Prerequisites"),
    ("timeline_order", "TimelineOrder"),
]
_LIST_FIELDS = {"Prerequisites"}  # land as a UE TArray<FString>
_OPTIONAL_SCALARS = {"TimelineOrder"}


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower()).strip("_") or "untitled"


def default_row_name(artifact: dict[str, Any]) -> str:
    """Stable DataTable row name (FName) from the quest title, e.g. 'Quest_smoke_over_the_marsh'.
    Deriving it from the title makes repeated landings of the same quest an idempotent upsert."""
    return "Quest_" + _slug(artifact.get("title", ""))


def quest_to_fields(artifact: dict[str, Any]) -> dict[str, Any]:
    """Quest dict -> UE Row Struct fields. Scalars are stringified (UE FString/FText); the
    prerequisites list is kept as a JSON array (UE TArray<FString>)."""
    fields: dict[str, Any] = {}
    for src, dst in _FIELD_MAP:
        value = artifact.get(src)
        if dst in _LIST_FIELDS:
            fields[dst] = [str(v) for v in (value or [])]
        elif dst in _OPTIONAL_SCALARS and value is None:
            continue
        else:
            fields[dst] = "" if value is None else str(value)
    return fields


def fields_to_quest(fields: dict[str, Any]) -> dict[str, Any]:
    """Inverse of `quest_to_fields` — turn a read-back DataTable row into a Quest-shaped dict so
    the landed row can be re-validated against the World Bible (engine-layer VERIFY)."""
    inverse = {dst: src for src, dst in _FIELD_MAP}
    quest: dict[str, Any] = {}
    for dst, value in (fields or {}).items():
        key = inverse.get(dst)
        if key == "prerequisites":
            quest[key] = list(value or [])
        elif key == "timeline_order":
            if value in (None, ""):
                continue
            quest[key] = int(value) if str(value).lstrip("-").isdigit() else value
        elif key is not None:
            quest[key] = value
    quest.setdefault("prerequisites", [])
    return quest


class UnrealAdapter(BaseEngineAdapter):
    name = "unreal"

    def __init__(
        self,
        bridge: UnrealBridge | None = None,
        *,
        table: str = "QuestTable",
        row_name_fn: Callable[[dict[str, Any]], str] | None = None,
        commit: bool = False,
        allowed_tables: set[str] | None = None,
    ):
        allowed = allowed_tables if allowed_tables is not None else {"QuestTable"}
        if table not in allowed:
            raise ValueError(f"table {table!r} is not in the Unreal write allowlist")
        self.bridge: UnrealBridge = bridge if bridge is not None else FakeUnrealBridge()
        self.table = table
        self.row_name_fn = row_name_fn or default_row_name
        self.commit = commit
        self._last_row_name: str | None = None
        self._last_command: dict[str, Any] | None = None

    def apply(self, artifact: dict[str, Any]) -> None:
        """Land the quest as a DataTable row via the bridge (idempotent upsert on the row name)."""
        row_name = self.row_name_fn(artifact)
        fields = quest_to_fields(artifact)
        self._last_command = {"table": self.table, "row_name": row_name, "fields": fields}
        if self.commit:
            self.bridge.upsert_datatable_row(self.table, row_name, fields)
        self._last_row_name = row_name

    def snapshot(self) -> dict[str, Any]:
        """Read the landed row back from the engine — extends VERIFY to the engine layer."""
        row = (
            self.bridge.read_datatable_row(self.table, self._last_row_name)
            if self._last_row_name is not None
            else None
        )
        return {
            "engine": self.name,
            "table": self.table,
            "row_name": self._last_row_name,
            "row": row,
            "committed": self.commit,
            "command": self._last_command,
        }
