# 在 UE 进程内执行:把导出的 demo JSON 灌进**持久**的 DataTable 资产并保存(供蓝图引用)。
#   与 ue_verify_import.py 的区别:那个建临时表验完即删,这个留在工程里。
"""In-engine half of ``install_demo_tables.py``.

Fills persistent demo DataTables under ``/Game/OWCopilot/Demo`` and saves them.
"""

from __future__ import annotations

import os

import unreal  # type: ignore[import-not-found]  # 仅在 UE 进程内可用

# 表名 -> 行结构体资产名(与 scripts/ue_verify_import.py 一致)
_TABLES = {
    "DialogueTreeNodes": "S_OWCDialogueNodeRow",
    "DialogueTreeChoices": "S_OWCDialogueChoiceRow",
    "QuestLogicVars": "S_OWCQuestLogicVarRow",
    "AuditFindings": "S_OWCAuditFindingRow",
}
_STRUCT_DIR = "/Game/OWCopilot/Structs"


def main() -> None:
    export_dir = os.environ.get("OWC_EXPORT_DIR", "")
    dest = os.environ.get("OWC_DEST", "/Game/OWCopilot/Demo").rstrip("/")
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()

    for table, struct_name in _TABLES.items():
        source = os.path.join(export_dir, f"{table}.json")
        if not os.path.isfile(source):
            unreal.log(f"OWC_INSTALL {table}: 导出目录内无此表,跳过")
            continue

        struct_path = f"{_STRUCT_DIR}/{struct_name}"
        row_struct = unreal.EditorAssetLibrary.load_asset(struct_path)
        if row_struct is None:
            unreal.log(f"OWC_INSTALL {table}: 行结构体缺失 {struct_path}")
            continue

        asset_path = f"{dest}/DT_{table}"
        if unreal.EditorAssetLibrary.does_asset_exist(asset_path):
            unreal.EditorAssetLibrary.delete_asset(asset_path)  # 重装即整表替换,避免残留旧行

        factory = unreal.DataTableFactory()
        factory.struct = row_struct
        table_asset = asset_tools.create_asset(f"DT_{table}", dest, unreal.DataTable, factory)
        if table_asset is None:
            unreal.log(f"OWC_INSTALL {table}: 资产创建失败")
            continue

        with open(source, encoding="utf-8") as handle:
            filled = unreal.DataTableFunctionLibrary.fill_data_table_from_json_string(
                table_asset, handle.read(), row_struct
            )
        rows = len(unreal.DataTableFunctionLibrary.get_data_table_row_names(table_asset))
        saved = unreal.EditorAssetLibrary.save_loaded_asset(table_asset)
        unreal.log(f"OWC_INSTALL {table}: fill={filled} rows={rows} saved={saved} -> {asset_path}")


main()
