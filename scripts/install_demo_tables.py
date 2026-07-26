# 把 demo 内容导出并装进 UE 工程的 DataTable 资产(留在工程里供蓝图引用,不是验证用的临时表)。
#   内容改了就重跑这一条命令,不必手动导出再手动导入。
"""Install the demo DataTables into a real UE project.

Exports ``demo/honglou`` in blueprint-friendly form (audit findings included) and fills persistent
``UDataTable`` assets under ``/Game/OWCopilot/Demo`` so the UMG blueprint can reference them.
Unlike ``run_ue_verify.py`` — which creates throwaway tables and deletes them — these assets stay.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
_IN_ENGINE = _HERE / "ue_install_demo.py"
_DEFAULT_ENGINE = Path(r"F:\UE5\UE_5.7")
_DEFAULT_PROJECT = Path(r"F:\UE5\project\OWCopilotDemo\OWCopilotDemo\OWCopilotDemo.uproject")


def _export(content_root: Path, out_dir: Path) -> int:
    """导出 blueprint_friendly 变体 + 审计发现表。返回 finding 条数。"""
    sys.path.insert(0, str(_REPO / "src"))
    from owcopilot.app._common import open_project  # noqa: PLC0415  (需先注入 src 路径)
    from owcopilot.exporters.unreal_datatable import export_unreal_datatables  # noqa: PLC0415
    from owcopilot.pipeline.export_gate import audit_findings_for_export  # noqa: PLC0415

    with open_project(str(content_root), sqlite_path=":memory:") as project:
        findings = list(audit_findings_for_export(project))
        export_unreal_datatables(
            project.bundle, out_dir, blueprint_friendly=True, audit_findings=findings
        )
    return len(findings)


def _installed_lines(text: str) -> list[str]:
    return [
        line.split("OWC_INSTALL ", 1)[1].strip()
        for line in text.splitlines()
        if "OWC_INSTALL " in line
    ]


def _latest_log(project: Path) -> str:
    logs = sorted(
        (project.parent / "Saved" / "Logs").glob("*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not logs:
        return ""
    return logs[0].read_text(encoding="utf-8", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser(description="把 demo 内容装进 UE 工程的 DataTable")
    parser.add_argument("--content-root", default=str(_REPO / "demo" / "honglou"))
    parser.add_argument("--project", default=str(_DEFAULT_PROJECT))
    parser.add_argument("--engine", default=str(_DEFAULT_ENGINE))
    parser.add_argument("--dest", default="/Game/OWCopilot/Demo", help="资产落点(/Game 路径)")
    args = parser.parse_args()

    editor = Path(args.engine) / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    if not editor.is_file():
        print(f"[错误] 找不到无头编辑器: {editor}", file=sys.stderr)
        return 2
    project = Path(args.project)
    if not project.is_file():
        print(f"[错误] 工程不存在: {project}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="owc_demo_export_") as staging:
        export_dir = Path(staging)
        count = _export(Path(args.content_root), export_dir)
        print(f"已导出({count} 条审计发现): {export_dir}")

        completed = subprocess.run(
            [
                str(editor),
                str(project),
                "-run=pythonscript",
                f"-script={_IN_ENGINE}",
                "-unattended",
                "-nullrhi",
                "-nosplash",
                "-nosound",
                "-NoShaderCompile",
            ],
            env={**os.environ, "OWC_EXPORT_DIR": str(export_dir), "OWC_DEST": args.dest},
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

    # unreal.log 只落工程日志文件,不进 stdout,所以结果要从最新一份日志里捞
    installed = _installed_lines(completed.stdout) or _installed_lines(_latest_log(project))
    for line in installed:
        print(f"  {line}")
    if not installed:
        print(completed.stdout[-3000:], file=sys.stderr)
        print("[错误] UE 未报告任何安装结果,查工程 Saved/Logs/", file=sys.stderr)
        return 1
    failed = [line for line in installed if "fill=True" not in line]
    if failed:
        print(f"[错误] 有表未成功灌入: {failed}", file=sys.stderr)
        return 1
    print(f"完成:{len(installed)} 张表已装入 {args.dest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
