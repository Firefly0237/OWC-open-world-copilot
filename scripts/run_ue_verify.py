# 宿主侧驱动:导出 blueprint_friendly 变体 → 启动无头 UE 跑 scripts/ue_verify_import.py →
#   读回机器可读报告并以退出码表态。让"真机验证"变成一条可反复执行、可进 CI 的命令,
#   而不是一次性的手工操作。
"""Host-side driver for the headless UE import verification.

Runs the whole loop in one command: export the blueprint-friendly tables, launch
``UnrealEditor-Cmd.exe`` on a scratch project, and assert on the JSON report the in-engine script
writes. Exits non-zero when any table fails to round-trip, so CI can gate on it.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_DEFAULT_ENGINE = Path(r"F:\UE5\UE_5.7")
_MINIMAL_UPROJECT = {
    "FileVersion": 3,
    "EngineAssociation": "5.7",
    "Category": "",
    "Description": "OWCopilot headless verification project",
    "Plugins": [{"Name": "PythonScriptPlugin", "Enabled": True}],
}


def _ensure_project(project: Path) -> Path:
    """保证有一个能被无头打开的最小工程(只需 .uproject + Content/,其余 UE 自己生成)。"""
    project.parent.mkdir(parents=True, exist_ok=True)
    (project.parent / "Content").mkdir(exist_ok=True)
    if not project.is_file():
        project.write_text(json.dumps(_MINIMAL_UPROJECT, indent=1), encoding="utf-8")
    return project


def _run_editor(engine: Path, project: Path, script: Path, env: dict[str, str]) -> int:
    editor = engine / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    if not editor.is_file():
        print(f"[错误] 找不到无头编辑器: {editor}", file=sys.stderr)
        return 2
    command = [
        str(editor),
        str(project),
        "-run=pythonscript",
        f"-script={script}",
        "-unattended",
        "-nullrhi",
        "-nosplash",
        "-nosound",
        "-NoShaderCompile",
    ]
    # UE 的输出不是本地代码页(Windows 上默认 GBK 会直接抛 UnicodeDecodeError),强制 UTF-8 + 替换
    completed = subprocess.run(
        command,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        print(completed.stdout[-4000:], file=sys.stderr)
        print(completed.stderr[-4000:], file=sys.stderr)
    return completed.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="无头 UE 导入验证")
    parser.add_argument(
        "--export-dir", required=True, help="blueprint_friendly 导出的 unreal/ 目录"
    )
    parser.add_argument(
        "--project", required=True, help="UE 工程 .uproject 路径(不存在则自动建最小工程)"
    )
    parser.add_argument("--engine", default=str(_DEFAULT_ENGINE), help="UE 引擎根目录")
    parser.add_argument(
        "--struct-dir", default="/Game/OWCopilot/Structs", help="人工手建行结构体所在的 /Game 路径"
    )
    parser.add_argument("--report", default="", help="报告输出路径(默认写到工程 Saved/ 下)")
    args = parser.parse_args()

    # 空串 resolve() 会静默变成当前工作目录且 is_dir() 为真,必须先挡掉,否则会跑去仓库根找表
    if not args.export_dir.strip():
        print("[错误] --export-dir 为空", file=sys.stderr)
        return 2
    export_dir = Path(args.export_dir).resolve()
    if not export_dir.is_dir():
        print(f"[错误] 导出目录不存在: {export_dir}", file=sys.stderr)
        return 2
    absent = [
        name
        for name in ("DialogueTreeNodes", "DialogueTreeChoices")
        if not (export_dir / f"{name}.json").is_file()
    ]
    if absent:
        print(
            f"[错误] {export_dir} 下缺少导出表 {absent};"
            "确认这是 blueprint_friendly 的 unreal 导出目录",
            file=sys.stderr,
        )
        return 2

    project = _ensure_project(Path(args.project).resolve())
    report = (
        Path(args.report).resolve() if args.report else project.parent / "Saved" / "owc_verify.json"
    )
    report.parent.mkdir(parents=True, exist_ok=True)
    if report.exists():
        report.unlink()  # 别把上一轮的结果当本轮的

    code = _run_editor(
        Path(args.engine),
        project,
        Path(__file__).with_name("ue_verify_import.py"),
        {
            "OWC_EXPORT_DIR": str(export_dir),
            "OWC_STRUCT_DIR": args.struct_dir,
            "OWC_REPORT": str(report),
        },
    )
    if not report.is_file():
        print(
            f"[错误] UE 未产出报告(退出码 {code});多半是脚本在引擎内抛错,查工程 Saved/Logs/",
            file=sys.stderr,
        )
        return 2

    payload = json.loads(report.read_text(encoding="utf-8"))
    print(f"引擎版本: {payload.get('engine_version')}")
    for table in payload.get("tables", []):
        mark = "OK  " if table.get("ok") else "FAIL"
        detail = table.get("error") or (
            f"源 {table.get('source_rows')} 行 / 读回 {table.get('readback_rows')} 行"
            f" ｜ 标量不符 {table.get('scalar_value_mismatch_count', 0)}"
            f" ｜ 结构体缺列 {table.get('fields_dropped_by_row_struct') or '无'}"
        )
        print(f"  [{mark}] {table.get('table')}: {detail}")
    print(f"报告: {report}")
    return 0 if payload.get("all_ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
