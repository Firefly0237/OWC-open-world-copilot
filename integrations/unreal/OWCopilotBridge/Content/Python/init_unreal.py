# 编辑器启动自动执行：把 OWCopilot 导入器挂到主菜单（零 C++ 的自注册钩子）。
"""``init_unreal.py`` — auto-run by the Python Editor Script Plugin at editor startup.

The editor adds every enabled plugin's ``Content/Python`` to ``sys.path`` and runs any
``init_unreal.py`` it finds there. That is the officially sanctioned zero-C++ self-registration
hook: this file installs an ``OWCopilot`` main-menu entry that drives ``owcopilot_bridge`` in-edit.

Editor-only. ``import unreal`` is guarded so the file is import-safe under CI / a plain venv (where
it simply does nothing). The folder picker is intentionally a project-configured path rather than a
faked dialog API: point ``OWCOPILOT_UE_EXPORT_DIR`` at the exported ``unreal/`` directory and,
optionally, ``OWCOPILOT_UE_TABLE_MAP`` at a JSON mapping ``{"Quests": "/Game/.../QuestTable"}``.
The exact in-editor file-dialog / Editor Utility Widget surface is verifiable only on a real machine
(see docs/UE_INTEGRATION.md).
"""

from __future__ import annotations

import json
import os

import owcopilot_bridge

try:  # editor-only; import-safe without the engine
    import unreal  # type: ignore[import-not-found]
except ImportError:
    unreal = None  # noqa: N816

_MAIN_MENU = "LevelEditor.MainMenu"
_OW_MENU = "LevelEditor.MainMenu.OWCopilot"


def _notify(message: str) -> None:
    if unreal is not None:
        unreal.log(f"[OWCopilot] {message}")


def _run_import() -> None:
    export_dir = os.environ.get("OWCOPILOT_UE_EXPORT_DIR", "")
    map_path = os.environ.get("OWCOPILOT_UE_TABLE_MAP", "")
    if not export_dir:
        _notify("请设置环境变量 OWCOPILOT_UE_EXPORT_DIR 指向导出的 unreal/ 目录后重试。")
        return
    table_map: dict[str, str] = {}
    if map_path and os.path.isfile(map_path):
        with open(map_path, encoding="utf-8") as handle:
            table_map = json.load(handle)
    result = owcopilot_bridge.import_bundle(export_dir, table_map)
    _notify(result.message)
    for table in result.tables:
        if not table.ok:
            _notify(f"  失败：{table.table_path} — {table.message}")


def _register_menu() -> None:
    """Register the OWCopilot main-menu entry (order-sensitive: run after ToolMenus is up)."""
    menus = unreal.ToolMenus.get()
    main_menu = menus.find_menu(_MAIN_MENU)
    if main_menu is None:
        return
    main_menu.add_sub_menu(
        owner="OWCopilot", section_name="", name="OWCopilot", label="OWCopilot"
    )

    @unreal.uclass()
    class _OWImportEntry(unreal.ToolMenuEntryScript):
        @unreal.ufunction(override=True)
        def execute(self, context: object) -> None:
            del context  # UE callback signature; the entry needs no per-invocation context
            _run_import()

    entry = _OWImportEntry()
    entry.init_entry(
        owner_name="OWCopilot",
        menu=_OW_MENU,
        section="",
        name="ImportContentBundle",
        label="Import content bundle into DataTable...",
        tool_tip="用 OWCopilot 导出的 DataTable JSON 更新工程内的 DataTable 资产",
    )
    ow_menu = menus.find_menu(_OW_MENU)
    if ow_menu is not None:
        ow_menu.add_menu_entry_object(entry)
    menus.refresh_all_widgets()


if unreal is not None:
    _register_menu()
