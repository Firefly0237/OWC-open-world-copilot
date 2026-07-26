# 在 UE 编辑器内把导出的 DataTable JSON 落库（read-merge-refill，结构化返回不抛裸栈）。
"""The in-editor importer: land exported DataTable rows into project DataTable assets.

Runs inside Unreal's Python (``import unreal`` is guarded so this module still imports under CI with
no engine). Every entry point returns a dataclass result instead of raising a raw traceback, so the
menu button and the headless remote transports all get a guided outcome.

Write path (per the Epic Python API):
  * READ current rows with ``DataTableFunctionLibrary.export_data_table_to_json_string`` — the
    documented read method (there is no single-call "table as JSON" reflected to Python, which is
    the bug the removed orphan helper carried);
  * MERGE by row ``Name`` (``merge.merge_rows_by_name``), preserving untouched rows;
  * WRITE with ``DataTableFunctionLibrary.fill_data_table_from_json_string`` — a DESTRUCTIVE
    empty-and-fill that returns a ``bool`` and only logs on failure, so the return value MUST be
    checked and surfaced;
  * pass the table's Row Struct as ``import_row_struct`` to force an automated (dialog-free) import;
  * ``EditorAssetLibrary.save_loaded_asset`` to persist the ``.uasset``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .bundle import plan_bundle_import
from .merge import merge_rows_by_name

try:  # editor-only; importing outside Unreal (CI, this venv) must not explode
    import unreal  # type: ignore[import-not-found]
except ImportError:
    unreal = None  # noqa: N816


@dataclass(frozen=True)
class ImportResult:
    ok: bool
    table_path: str
    rows_written: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    message: str = ""
    error: str = ""


@dataclass(frozen=True)
class BundleImportResult:
    ok: bool
    unreal_dir: str
    tables: list[ImportResult] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    message: str = ""


_NO_ENGINE_MESSAGE = (
    "需在 Unreal 编辑器内运行（未检测到 `unreal` 模块）。"
    "请启用 Python Editor Script 插件后从 OWCopilot 菜单导入，"
    "或用 Remote Control / Remote Execution 传输从命令行推送。"
)


def _no_engine(table_path: str) -> ImportResult:
    return ImportResult(
        ok=False, table_path=table_path, message=_NO_ENGINE_MESSAGE, error="no_engine"
    )


def _read_current_rows(table: Any) -> list[dict[str, Any]]:
    raw = unreal.DataTableFunctionLibrary.export_data_table_to_json_string(table)
    if not raw:
        return []
    data = json.loads(raw)
    return data if isinstance(data, list) else []


def _resolve_row_struct(table: Any, import_row_struct: Any) -> Any:
    """Force an automated import by supplying the table's Row Struct (headless = no dialog)."""
    if import_row_struct is not None:
        return import_row_struct
    return table.get_editor_property("RowStruct")


def import_rows(
    table_path: str,
    rows: list[dict[str, Any]],
    *,
    import_row_struct: Any = None,
) -> ImportResult:
    """Upsert ``rows`` into the DataTable at ``table_path`` (read-merge-refill), then save.

    Returns a structured result; ``ok`` is False with a guided message if the engine is absent, the
    asset is missing, the merge input is malformed, ``fill_...`` returns False, or the save fails.
    """
    if unreal is None:
        return _no_engine(table_path)
    try:
        table = unreal.load_asset(table_path)
        if table is None:
            return ImportResult(
                ok=False,
                table_path=table_path,
                message=f"未找到 DataTable 资产：{table_path}。请在 UE 工程内创建该表并核对路径。",
                error="asset_not_found",
            )
        outcome = merge_rows_by_name(_read_current_rows(table), rows)
        struct = _resolve_row_struct(table, import_row_struct)
        json_string = json.dumps(outcome.rows, ensure_ascii=False)
        filled = unreal.DataTableFunctionLibrary.fill_data_table_from_json_string(
            table, json_string, struct
        )
        if not filled:
            return ImportResult(
                ok=False,
                table_path=table_path,
                added=outcome.added,
                updated=outcome.updated,
                unchanged=outcome.unchanged,
                message=(
                    "UE 拒绝了导入 JSON（fill_data_table_from_json_string 返回 False）。"
                    "常见原因：行结构字段名/类型与 JSON 不匹配、或引用了不存在的 RowStruct。"
                    "请对照 OWCopilotRows.h 核对 Row Struct，再重试。"
                ),
                error="fill_returned_false",
            )
        if not unreal.EditorAssetLibrary.save_loaded_asset(table):
            return ImportResult(
                ok=False,
                table_path=table_path,
                rows_written=len(outcome.rows),
                added=outcome.added,
                updated=outcome.updated,
                unchanged=outcome.unchanged,
                message="导入成功但保存失败（save_loaded_asset 返回 False），请检查资产是否只读",
                error="save_failed",
            )
    except ValueError as exc:  # malformed rows (missing Name) from the merge
        return ImportResult(
            ok=False,
            table_path=table_path,
            message=f"行数据不合法：{exc}",
            error="invalid_rows",
        )
    except Exception as exc:  # noqa: BLE001 — convert any engine error into a guided result
        return ImportResult(
            ok=False,
            table_path=table_path,
            message=(
                "导入 DataTable 时发生错误。请核对表路径、Row Struct 字段名/类型，"
                "以及编辑器是否已启用 Python Editor Script 插件。"
            ),
            error=str(exc),
        )
    return ImportResult(
        ok=True,
        table_path=table_path,
        rows_written=len(outcome.rows),
        added=outcome.added,
        updated=outcome.updated,
        unchanged=outcome.unchanged,
        message=(
            f"已导入 {table_path}：新增 {outcome.added}、更新 {outcome.updated}、"
            f"保留 {outcome.unchanged}，共写入 {len(outcome.rows)} 行。"
        ),
    )


def import_bundle(
    unreal_dir: str | Path,
    table_map: dict[str, str] | None = None,
    *,
    import_row_struct: Any = None,
) -> BundleImportResult:
    """Import every mapped DataTable JSON in an exported ``unreal/`` directory.

    Discovery/planning is pure (``plan_bundle_import``); unmapped tables are skipped with a warning
    rather than guessed. Aggregates per-table :class:`ImportResult`\\ s into a structured result.
    """
    plan = plan_bundle_import(unreal_dir, table_map)
    base = Path(unreal_dir)
    results: list[ImportResult] = []
    warnings = list(plan.warnings)

    for table in plan.tables:
        if not table.mapped:
            continue  # already reported as a warning by the plan
        try:
            rows = json.loads((base / table.file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            results.append(
                ImportResult(
                    ok=False,
                    table_path=table.table_path,
                    message=f"无法读取 {table.file}：{exc}",
                    error="read_failed",
                )
            )
            continue
        results.append(import_rows(table.table_path, rows, import_row_struct=import_row_struct))

    ok = bool(results) and all(result.ok for result in results)
    if not results:
        message = "没有可导入的表（检查 table_map 是否为发现的 DataTable 指定了目标资产路径）。"
    elif ok:
        message = f"已导入 {len(results)} 张 DataTable。"
    else:
        failed = sum(1 for result in results if not result.ok)
        message = f"导入完成但有 {failed}/{len(results)} 张表失败，详见各表结果。"
    return BundleImportResult(
        ok=ok,
        unreal_dir=str(base),
        tables=results,
        warnings=warnings,
        message=message,
    )
