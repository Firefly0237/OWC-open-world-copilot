# UE 真机导入回归门:把"导出的 DataTable 能被真实 Unreal 吃下并读得回来"变成一条可重复执行的
#   测试,而不是一次性的手工验证。没有引擎/未建行结构体时 skip(带明确原因),装了引擎就必须绿。
#   立此门的原因:本项目长期的诚实债就是"引擎侧零真机执行证据",手工验一次不固化就会重新欠上。
"""Live UE import regression gate.

Skipped unless a real engine *and* the hand-built row structs are present (``OWC_UE_PROJECT`` /
``OWC_UE_ENGINE``), so CI without an engine stays green and offline-deterministic. When the engine
is there, this is the test that proves the blueprint-friendly export still lands in a real
``UDataTable`` — export-format changes cannot silently break engine ingestion again.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from owcopilot.exporters.unreal_datatable import AuditFindingInput, export_unreal_datatables

from .test_exporters_unreal import _full_logic_bundle

_REPO = Path(__file__).resolve().parents[2]
_DRIVER = _REPO / "scripts" / "run_ue_verify.py"
_TABLES = ("DialogueTreeNodes", "DialogueTreeChoices", "QuestLogicVars", "AuditFindings")
_STRUCTS = (
    "S_OWCClause",
    "S_OWCDialogueNodeRow",
    "S_OWCDialogueChoiceRow",
    "S_OWCQuestLogicVarRow",
    "S_OWCAuditFindingRow",
)
# demo 的审计横幅要的形状:一条 finding 至少带规则码与命中节点,蓝图才能按当前节点过滤
_AUDIT_FINDING = AuditFindingInput(
    rule_code="DIALOGUE_UNGATED_SIBLING_CHOICE",
    severity="warning",
    target_ref="dialogue_tree:dt1",
    message="choice at nodes/n1/choices/0 is gated, its sibling is not",
    evidence_paths=("nodes/n1/choices/0", "nodes/n1/choices/1"),
)


def _engine_root() -> Path | None:
    configured = os.environ.get("OWC_UE_ENGINE") or r"F:\UE5\UE_5.7"
    editor = Path(configured) / "Engine" / "Binaries" / "Win64" / "UnrealEditor-Cmd.exe"
    return Path(configured) if editor.is_file() else None


def _project() -> Path | None:
    configured = os.environ.get("OWC_UE_PROJECT")
    if configured and Path(configured).is_file():
        return Path(configured)
    fallback = Path(r"F:\UE5\project\OWCopilotDemo\OWCopilotDemo\OWCopilotDemo.uproject")
    return fallback if fallback.is_file() else None


def _missing_structs(project: Path) -> list[str]:
    """行结构体是人工在编辑器里建的(Python 加不了字段),缺了就 skip 而不是判失败。"""
    struct_dir = project.parent / "Content" / "OWCopilot" / "Structs"
    return [name for name in _STRUCTS if not (struct_dir / f"{name}.uasset").is_file()]


def test_blueprint_friendly_export_lands_in_real_unreal(tmp_path: Path) -> None:
    engine = _engine_root()
    if engine is None:
        pytest.skip("未找到 UE 引擎(设 OWC_UE_ENGINE 指向引擎根目录后本门才生效)")
    project = _project()
    if project is None:
        pytest.skip("未找到 UE 工程(设 OWC_UE_PROJECT 指向 .uproject 后本门才生效)")
    absent = _missing_structs(project)
    if absent:
        pytest.skip(
            f"工程内缺少人工手建的行结构体 {absent}"
            "(见 scripts/ue_verify_import.py)"
        )

    export_dir = tmp_path / "export"
    export_unreal_datatables(
        _full_logic_bundle(),
        export_dir,
        blueprint_friendly=True,
        audit_findings=[_AUDIT_FINDING],
    )
    for table in _TABLES:
        assert (export_dir / f"{table}.json").is_file(), f"导出缺表 {table}"

    report = tmp_path / "owc_verify.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(_DRIVER),
            "--export-dir",
            str(export_dir),
            "--project",
            str(project),
            "--engine",
            str(engine),
            "--report",
            str(report),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
    )
    assert report.is_file(), (
        f"UE 未产出验证报告:\n{completed.stdout[-2000:]}\n{completed.stderr[-2000:]}"
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    failures = [t for t in payload["tables"] if not t.get("ok")]
    assert not failures, "真机导入失败:\n" + json.dumps(failures, ensure_ascii=False, indent=2)
    assert payload["all_ok"] is True
    assert completed.returncode == 0

    # 逐表复核关键结论,避免报告结构变了却仍"全绿"
    by_table = {t["table"]: t for t in payload["tables"]}
    assert set(by_table) == set(_TABLES)
    for table in _TABLES:
        entry = by_table[table]
        assert entry["source_rows"] == entry["readback_rows"] > 0, f"{table} 行数对不上"
        assert not entry["fields_dropped_by_row_struct"], f"{table} 有列没被行结构体接住"
        assert entry["scalar_value_mismatch_count"] == 0, f"{table} 标量值不符"
        assert not entry["complex_columns_unexpectedly_empty"], f"{table} 复杂列该有值却空"

    # B1/B2 的三个新列必须真的到达引擎侧(这是本轮改动的核心交付)
    choices_columns = by_table["DialogueTreeChoices"]["column_names"]
    for column in ("ConditionClauses", "ConditionCompilable", "EffectsStructured"):
        assert column in choices_columns, f"UE 侧未认到新列 {column}"
