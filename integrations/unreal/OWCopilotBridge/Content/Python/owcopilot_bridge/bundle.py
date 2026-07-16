# 发现并规划一个导出目录里的 DataTable JSON（纯逻辑，离线可验证）。
"""Discover the DataTable JSON files OWCopilot's exporter wrote and plan their import.

The exporter (``exporters/unreal_datatable.py``) writes an ``unreal/`` directory containing one
DataTable JSON per table (``Quests.json`` / ``Entities.json`` / ``Dialogues.json``), plus companion
StringTable CSVs, a row-struct header, and ``unreal_preflight.json``. A DataTable JSON is, by the
documented import contract, a *top-level array of row objects each carrying a ``Name`` key* — that
shape is self-describing, so we recognise DataTables by it rather than hard-coding filenames. The
preflight object (a JSON object, not an array) is excluded naturally.

Planning is pure and stdlib-only: it reads the directory and resolves each table's target DataTable
asset path from a supplied ``table_map`` (``{"Quests": "/Game/.../QuestTable", ...}``). No engine is
needed, so this is the offline-verifiable half of ``import_bundle``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class TablePlan:
    """One DataTable JSON discovered in the export dir and its resolved target asset."""

    file: str  # source filename, e.g. "Quests.json"
    table_path: str  # resolved DataTable asset path, or "" when unmapped
    row_count: int
    mapped: bool


@dataclass(frozen=True)
class BundleImportPlan:
    unreal_dir: str
    tables: list[TablePlan] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _is_datatable(data: Any) -> bool:
    """True iff ``data`` is the DataTable import shape: a non-empty array of ``Name``-keyed rows."""
    return (
        isinstance(data, list)
        and len(data) > 0
        and all(
            isinstance(item, dict) and isinstance(item.get("Name"), str) and bool(item.get("Name"))
            for item in data
        )
    )


def _resolve_table_path(stem: str, filename: str, table_map: dict[str, str]) -> str:
    """A table_map may key by stem ("Quests") or full filename ("Quests.json")."""
    return table_map.get(stem) or table_map.get(filename) or ""


def plan_bundle_import(
    unreal_dir: str | Path, table_map: dict[str, str] | None = None
) -> BundleImportPlan:
    """Scan an exported ``unreal/`` directory and plan which DataTables to import where.

    Non-DataTable JSON (e.g. ``unreal_preflight.json``) is skipped. Tables without a ``table_map``
    entry are reported as unmapped warnings and will be skipped by ``import_bundle`` rather than
    guessed.
    """
    resolved_map = table_map or {}
    base = Path(unreal_dir)
    tables: list[TablePlan] = []
    warnings: list[str] = []

    if not base.is_dir():
        return BundleImportPlan(
            unreal_dir=str(base),
            warnings=[f"导出目录不存在或不是目录：{base}"],
        )

    for path in sorted(base.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"跳过无法读取/解析的 JSON：{path.name}（{exc}）")
            continue
        if not _is_datatable(data):
            continue  # preflight/manifest objects and empty files are not DataTables
        table_path = _resolve_table_path(path.stem, path.name, resolved_map)
        mapped = bool(table_path)
        if not mapped:
            warnings.append(
                f"未为 {path.name} 指定目标 DataTable 资产路径"
                f"（table_map 缺 '{path.stem}'），导入时将跳过该表"
            )
        tables.append(
            TablePlan(file=path.name, table_path=table_path, row_count=len(data), mapped=mapped)
        )

    if not tables:
        warnings.append(
            f"目录内未发现 DataTable JSON（应为按 Name 键控的行对象数组）：{base}"
        )
    return BundleImportPlan(unreal_dir=str(base), tables=tables, warnings=warnings)
