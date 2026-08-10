# UE 无头导入验证脚本(在 UnrealEditor-Cmd.exe 的 pythonscript 里执行):把 blueprint_friendly
#   导出的三张表真正灌进真实 UE 的 DataTable 资产,再读回来逐行逐字段比对,产出机器可读报告。
#   这是把"零真机执行证据"变成证据的那件工具——不依赖 GUI,可反复跑、可进 CI。
#
# 前置:行结构体必须已由人工在编辑器里建好(UE 5.7 的 Python 没有 UserDefinedStructFactory,
#   实测确认无法脚本创建),见 scripts/ue_verify_import.py。
#
# 用法(宿主侧见 scripts/run_ue_verify.py):
#   UnrealEditor-Cmd.exe <proj>.uproject -run=pythonscript -script=<本文件> \
#       -unattended -nullrhi -nosplash -nosound
#   环境变量:OWC_EXPORT_DIR / OWC_STRUCT_DIR / OWC_REPORT
"""Headless UE verification of the blueprint-friendly DataTable export.

Fills real ``UDataTable`` assets from our exported JSON and reads them back, so the round-trip is
proven **inside the engine** rather than asserted offline. Writes a JSON report so the host side
(and CI) can assert on it without scraping the UE log.
"""

from __future__ import annotations

import json
import os
import re
import traceback

import unreal  # type: ignore[import-not-found]  # 仅在 UE 进程内可用

# 表名 -> 人工手建的行结构体资产名(与 scripts/ue_verify_import.py 一致)
_TABLES = {
    "DialogueTreeNodes": "S_OWCDialogueNodeRow",
    "DialogueTreeChoices": "S_OWCDialogueChoiceRow",
    "QuestLogicVars": "S_OWCQuestLogicVarRow",
    # 审计发现表是 opt-in 的(调用方传了 audit_findings 才产出),导出目录里没有就跳过而非判失败
    "AuditFindings": "S_OWCAuditFindingRow",
}
_OPTIONAL_TABLES = frozenset({"AuditFindings"})

_STAGING = "/Game/OWCopilot/_verify"  # 验证用的临时 DataTable 落点(每次重建)


_FTEXT_FORMS = (
    # UE 把 FText 列读成可复现的本地化字面量;第三个参数才是源串,前两个是命名空间与键。
    re.compile(
        r'^NSLOCTEXT\(\s*"(?:[^"\\]|\\.)*"\s*,\s*"(?:[^"\\]|\\.)*"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)$'
    ),
    re.compile(r'^LOCTEXT\(\s*"(?:[^"\\]|\\.)*"\s*,\s*"((?:[^"\\]|\\.)*)"\s*\)$'),
    re.compile(r'^INVTEXT\(\s*"((?:[^"\\]|\\.)*)"\s*\)$'),
)


def _unwrap_ftext(text: str) -> str:
    """把 ``NSLOCTEXT("ns","key","原文")`` 这类 FText 字面量还原成原文。

    导入 FText 列时 UE 会按 行名_列名 生成本地化键并保留源串,读回即是这种包裹形态——
    它是正确行为而非数据错,所以比对前先拆包,否则每条台词都会被误判成不符。
    """
    stripped = text.strip()
    for pattern in _FTEXT_FORMS:
        matched = pattern.match(stripped)
        if matched:
            return _unescape(matched.group(1))
    return text


def _unescape(literal: str) -> str:
    """还原 UE 字面量里的反斜杠转义。

    UE 转义的不止双引号——台词里的单引号会写成 ``\\'``(实测:「当不得'读书'二字」)。逐字符扫描
    比逐个 ``replace`` 可靠:反斜杠后的任何字符都取其字面值,``\\\\`` 也就自然只吃掉一层。
    """
    out: list[str] = []
    escaped = False
    for char in literal:
        if escaped:
            out.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        else:
            out.append(char)
    if escaped:  # 结尾孤立的反斜杠按字面保留
        out.append("\\")
    return "".join(out)


def _canon(value: object) -> str:
    """把源侧的 JSON 值与读回侧的 UE 字符串拉到同一个可比形态。

    读回走的是 ``get_column_as_string``,拿到的一律是 UE 的字符串显示形态(布尔是 ``True``/
    ``False``、整数是 ``0``),而源侧是 JSON 字面量。故两侧统一转成小写去空白的字符串再比。
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return _unwrap_ftext(str(value)).strip().lower()


def _rows_by_name(rows: list[dict]) -> dict[str, dict]:
    return {str(r.get("Name", "")): r for r in rows}


def _compare(source: list[dict], readback: list[dict]) -> dict:
    """逐行逐字段比对。只比"两侧都有的字段",缺列单独列出,避免把结构体少建一列误判成数据错。"""
    src, back = _rows_by_name(source), _rows_by_name(readback)
    missing = sorted(set(src) - set(back))
    extra = sorted(set(back) - set(src))
    mismatches: list[dict] = []
    dropped_fields: set[str] = set()

    presence_only: set[str] = set()  # 数组/嵌套列只验"源有值则读回也非空",不做逐字比对
    empty_complex: list[dict] = []

    for name in sorted(set(src) & set(back)):
        s_row, b_row = src[name], back[name]
        dropped_fields |= {k for k in s_row if k != "Name" and k not in b_row}
        for key, s_val in s_row.items():
            if key == "Name" or key not in b_row:
                continue
            if isinstance(s_val, (list, dict)):
                presence_only.add(key)
                # UE 把数组显示成 ((Var="x",...)) 这类聚合串,逐字比没有意义;只要源非空读回也非空
                if s_val and not str(b_row[key]).strip("() "):
                    empty_complex.append({"row": name, "field": key, "expected": s_val})
                continue
            if _canon(s_val) != _canon(b_row[key]):
                mismatches.append(
                    {"row": name, "field": key, "expected": s_val, "actual": b_row[key]}
                )

    return {
        "source_rows": len(src),
        "readback_rows": len(back),
        "rows_missing_after_import": missing,
        "rows_unexpected_after_import": extra,
        "fields_dropped_by_row_struct": sorted(dropped_fields),
        "scalar_value_mismatches": mismatches[:40],
        "scalar_value_mismatch_count": len(mismatches),
        "presence_only_columns": sorted(presence_only),
        "complex_columns_unexpectedly_empty": empty_complex[:20],
    }


def _readback_by_column(
    table_asset: object, row_names: list[str], columns: list[str]
) -> list[dict]:
    """逐列把表内容读成字符串,拼回 [{Name, 列: 值}] 形态。

    ``get_column_as_string`` 返回该列按行序排列的字符串数组,行序与 ``get_data_table_row_names``
    一致,据此还原每一行。值一律是字符串(UE 的显示形态),所以比对时两侧都按字符串归一。
    """
    rows: list[dict] = [{"Name": name} for name in row_names]
    for column in columns:
        try:
            values = [str(v) for v in table_asset.get_column_as_string(column)]
        except Exception:
            continue  # 该列取不到就跳过,由 fields_dropped 一侧体现
        for index, value in enumerate(values[: len(rows)]):
            rows[index][column] = value
    return rows


def _verify_table(table: str, struct_name: str, export_dir: str, struct_dir: str) -> dict:
    result: dict = {"table": table, "struct": struct_name, "ok": False}

    json_path = os.path.join(export_dir, f"{table}.json")
    if not os.path.isfile(json_path):
        if table in _OPTIONAL_TABLES:
            result["skipped"] = f"本次导出未产出可选表 {table}.json"
            result["ok"] = True
            return result
        result["error"] = f"导出文件不存在: {json_path}"
        return result

    struct_path = f"{struct_dir.rstrip('/')}/{struct_name}"
    if not unreal.EditorAssetLibrary.does_asset_exist(struct_path):
        result["error"] = f"行结构体未找到: {struct_path}(请先按 UE结构体手建清单.md 建好并保存)"
        return result

    row_struct = unreal.EditorAssetLibrary.load_asset(struct_path)
    if row_struct is None:
        result["error"] = f"行结构体加载失败: {struct_path}"
        return result

    with open(json_path, encoding="utf-8") as handle:
        source_text = handle.read()
    source_rows = json.loads(source_text)

    # 上一轮保存过的同名资产仍挂在内存里时,delete_asset 删不净、create_asset 会直接返回 None
    # (表现为"资产创建失败")。故用 create_unique_asset_name 每次取一个没被占用的名字。
    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()
    unique_path, unique_name = asset_tools.create_unique_asset_name(f"{_STAGING}/DT_{table}", "")
    factory = unreal.DataTableFactory()
    factory.struct = row_struct
    table_asset = asset_tools.create_asset(
        unique_name or f"DT_{table}", _STAGING, unreal.DataTable, factory
    )
    if table_asset is None:
        result["error"] = f"DataTable 资产创建失败(尝试路径 {unique_path})"
        return result
    result["asset"] = str(table_asset.get_path_name())

    filled = unreal.DataTableFunctionLibrary.fill_data_table_from_json_string(
        table_asset, source_text, row_struct
    )
    result["fill_returned"] = bool(filled)

    if not filled:
        result["error"] = "fill_data_table_from_json_string 返回 False —— 行结构体与导出列不匹配"
        return result

    result["saved"] = bool(unreal.EditorAssetLibrary.save_loaded_asset(table_asset))
    # 用户结构体的内部字段名带 GUID 后缀(Var_2_5A32...),与 JSON 对得上的是 export names
    result["column_names"] = [str(c) for c in table_asset.get_column_export_names()]

    row_names = [
        str(n) for n in unreal.DataTableFunctionLibrary.get_data_table_row_names(table_asset)
    ]
    if not row_names:
        result["error"] = "灌入后表内 0 行"
        return result

    # 读回刻意不用 export_data_table_to_json_string:UE 5.7 的 JSON 写出器在本工程的行结构上
    # 会断言崩溃(JsonWriter.h:272),整个引擎进程一起死。逐列取字符串同样能证明数据真在表里,
    # 且不触碰那条脆弱路径。
    readback = _readback_by_column(table_asset, row_names, result["column_names"])
    diff = _compare(source_rows, readback)
    result.update(diff)
    result["ok"] = (
        bool(filled)
        and not diff["rows_missing_after_import"]
        and not diff["rows_unexpected_after_import"]
        and diff["scalar_value_mismatch_count"] == 0
        and not diff["complex_columns_unexpectedly_empty"]
    )
    return result


def main() -> None:
    export_dir = os.environ.get("OWC_EXPORT_DIR", "")
    struct_dir = os.environ.get("OWC_STRUCT_DIR", "/Game/OWCopilot/Structs")
    report_path = os.environ.get("OWC_REPORT", "")

    report: dict = {
        "engine_version": str(unreal.SystemLibrary.get_engine_version()),
        "export_dir": export_dir,
        "struct_dir": struct_dir,
        "tables": [],
    }
    for table, struct_name in _TABLES.items():
        try:
            report["tables"].append(_verify_table(table, struct_name, export_dir, struct_dir))
        except Exception:  # 单表失败不应拖垮整轮验证,把栈写进报告便于定位
            report["tables"].append(
                {
                    "table": table,
                    "struct": struct_name,
                    "ok": False,
                    "error": traceback.format_exc(),
                }
            )

    # 验证用的临时表不留在工程里,否则下一轮同名冲突、也污染用户的 Content Browser
    try:
        unreal.EditorAssetLibrary.delete_directory(_STAGING)
    except Exception:  # 清理失败不该让验证结论作废,只记进报告
        report["staging_cleanup_failed"] = True

    report["all_ok"] = bool(report["tables"]) and all(t.get("ok") for t in report["tables"])
    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if report_path:
        with open(report_path, "w", encoding="utf-8") as handle:
            handle.write(payload)
    for line in payload.splitlines():  # 同时打进 UE 日志,便于无报告文件时排查
        unreal.log(f"OWC_VERIFY {line}")


main()
