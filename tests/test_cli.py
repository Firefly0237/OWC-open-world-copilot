from __future__ import annotations

import json

from openpyxl import Workbook

from owcopilot.cli.main import build_parser, main
from owcopilot.content.models import (
    Branch,
    ContentBundle,
    Entity,
    EntityType,
    LocalizedText,
    Quest,
    QuestLogic,
    QuestStage,
)
from owcopilot.content.store import ContentStore


def _read_json(capsys) -> dict:
    return json.loads(capsys.readouterr().out)


def _read_stderr_json(capsys) -> dict:
    return json.loads(capsys.readouterr().err)


def test_cli_ingest_dry_run_and_write(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    source = tmp_path / "entities.json"
    source.write_text(
        json.dumps([{"kind": "entity", "id": "npc_aldric", "name": "Aldric", "type": "npc"}]),
        encoding="utf-8",
    )

    assert main(["ingest", "--content-root", str(content_root), "--input", str(source)]) == 0
    dry_run_body = _read_json(capsys)
    assert dry_run_body["dry_run"] is True
    assert dry_run_body["incoming_count"] == 1
    assert dry_run_body["cost_budget"]["used_usd"] == 0.0
    assert ContentStore(content_root).load().entities == {}

    assert (
        main(["ingest", "--content-root", str(content_root), "--input", str(source), "--write"])
        == 0
    )
    write_body = _read_json(capsys)
    assert write_body["dry_run"] is False
    assert "npc_aldric" in ContentStore(content_root).load().entities


def test_cli_ingest_uses_per_file_field_mapping_transactionally(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    faction_path = tmp_path / "阵营表.xlsx"
    relation_path = tmp_path / "阵营关系表.xlsx"
    mapping_path = tmp_path / "field_mapping.json"

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["编号", "名称", "类型"])
    sheet.append(["fac_a", "A", "faction"])
    sheet.append(["fac_b", "B", "faction"])
    workbook.save(faction_path)

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["来源阵营", "关系", "目标阵营"])
    sheet.append(["fac_a", "enemy_of", "fac_b"])
    workbook.save(relation_path)

    mapping_path.write_text(
        json.dumps(
            {
                "阵营表.xlsx": {
                    "type": "entity",
                    "columns": {"编号": "id", "名称": "name", "类型": "type"},
                },
                "阵营关系表.xlsx": {
                    "type": "relation",
                    "columns": {"来源阵营": "source", "关系": "kind", "目标阵营": "target"},
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ingest",
                "--content-root",
                str(content_root),
                "--field-mapping",
                str(mapping_path),
                "--input",
                str(faction_path),
                "--input",
                str(relation_path),
                "--write",
            ]
        )
        == 0
    )
    body = _read_json(capsys)
    bundle = ContentStore(content_root).load()

    assert body["incoming_count"] == 3
    assert "fac_a" in bundle.entities
    assert [(relation.source, relation.kind, relation.target) for relation in bundle.relations] == [
        ("fac_a", "enemy_of", "fac_b")
    ]


def test_cli_ingest_field_mapping_falls_back_to_matching_columns(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    task_path = tmp_path / "new_version_task.xlsx"
    mapping_path = tmp_path / "field_mapping.json"

    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["任务ID", "任务名", "接取NPC", "发生地点", "任务目标", "文本Key"])
    sheet.append(["q1", "New Quest", "npc_aldric", "poi_dock", "talk:npc_aldric", "QUEST_Q1"])
    workbook.save(task_path)
    mapping_path.write_text(
        json.dumps(
            {
                "02_配置表/任务表.xlsx": {
                    "type": "quest",
                    "columns": {
                        "任务ID": "id",
                        "任务名": "title",
                        "接取NPC": "giver_npc",
                        "发生地点": "location",
                        "任务目标": "objective",
                        "文本Key": "localization_keys",
                    },
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ingest",
                "--content-root",
                str(content_root),
                "--field-mapping",
                str(mapping_path),
                "--input",
                str(task_path),
                "--write",
            ]
        )
        == 0
    )
    _read_json(capsys)

    assert ContentStore(content_root).load().quests["q1"].objective == "talk:npc_aldric"


def test_cli_ingest_skip_conflicts_writes_non_conflicting_objects(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    source = tmp_path / "quests.json"
    ContentStore(content_root).save(
        ContentBundle(
            quests={
                "q1": Quest(
                    id="q1",
                    title="Original",
                    objective="keep existing",
                )
            }
        )
    )
    source.write_text(
        json.dumps(
            [
                {
                    "kind": "quest",
                    "id": "q1",
                    "title": "Changed",
                    "objective": "would overwrite",
                },
                {
                    "kind": "quest",
                    "id": "q2",
                    "title": "New",
                    "objective": "add this",
                },
            ]
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ingest",
                "--content-root",
                str(content_root),
                "--input",
                str(source),
                "--write",
                "--skip-conflicts",
            ]
        )
        == 0
    )
    body = _read_json(capsys)
    bundle = ContentStore(content_root).load()

    assert body["has_errors"] is True
    assert body["issues"][0]["rule_code"] == "IMPORT_CONFLICT"
    assert bundle.quests["q1"].title == "Original"
    assert bundle.quests["q2"].title == "New"


def test_cli_audit_persists_and_issues_lists_filters(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )

    assert main(["audit", "--content-root", str(content_root)]) == 0
    audit_body = _read_json(capsys)
    assert audit_body["open_errors"] >= 1
    assert "UNKNOWN_ENTITY_REF" in {issue["rule_code"] for issue in audit_body["issues"]}
    assert audit_body["cost_budget"]["used_usd"] == 0.0

    assert (
        main(
            [
                "issues",
                "--content-root",
                str(content_root),
                "--rule-code",
                "UNKNOWN_ENTITY_REF",
                "--status",
                "open",
            ]
        )
        == 0
    )
    issues_body = _read_json(capsys)
    assert issues_body["count"] == 1
    assert issues_body["issues"][0]["target_ref"] == "quest:q1"
    assert issues_body["cost_budget"]["used_usd"] == 0.0


def test_cli_quality_harness_reports_repair_loop_state(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )

    assert main(["quality-harness", "--content-root", str(content_root)]) == 0
    body = _read_json(capsys)

    assert body["phase"] == "repair"
    assert body["export_ready"] is False
    assert body["top_issues"]
    assert body["next_tool_calls"][0]["tool"] == "list_issues"
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_audit_fail_on_error_output_and_baseline(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    output_path = tmp_path / "reports" / "audit.json"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )

    exit_code = main(
        [
            "audit",
            "--content-root",
            str(content_root),
            "--fail-on-error",
            "--output",
            str(output_path),
        ]
    )
    audit_body = _read_json(capsys)
    assert exit_code == 1
    assert output_path.exists()
    assert json.loads(output_path.read_text(encoding="utf-8")) == audit_body

    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"fingerprints": [issue["fingerprint"] for issue in audit_body["issues"]]}),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "audit",
            "--content-root",
            str(content_root),
            "--fail-on-error",
            "--baseline",
            str(baseline),
        ]
    )
    suppressed_body = _read_json(capsys)
    assert exit_code == 0
    assert suppressed_body["open_errors"] == 0
    assert {issue["status"] for issue in suppressed_body["issues"]} == {"suppressed"}


def test_cli_context_pack_and_ask(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            entities={
                "npc_aldric": Entity(
                    id="npc_aldric",
                    name="Aldric",
                    type=EntityType.NPC,
                    description="Caravan master",
                )
            }
        )
    )

    assert (
        main(
            [
                "context-pack",
                "--content-root",
                str(content_root),
                "--query",
                "Aldric caravan",
            ]
        )
        == 0
    )
    context_body = _read_json(capsys)
    assert context_body["refs"] == ["entity:npc_aldric"]
    assert context_body["cost_budget"]["used_usd"] == 0.0

    assert main(["ask", "--content-root", str(content_root), "--query", "Who is Aldric?"]) == 0
    ask_body = _read_json(capsys)
    assert ask_body["answer"]["citations"][0]["ref"] == "entity:npc_aldric"
    assert ask_body["telemetry"]["calls"] == 1
    assert ask_body["cost_budget"]["over_budget"] is False

    assert (
        main(
            [
                "ask",
                "--content-root",
                str(content_root),
                "--query",
                "Who is Aldric?",
                "--max-cost-usd",
                "0",
            ]
        )
        == 0
    )
    over_budget_body = _read_json(capsys)
    assert over_budget_body["cost_budget"]["over_budget"] is True


def test_cli_export_writes_engine_scoped_bundle(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    ContentStore(content_root).save(
        ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)}
        )
    )

    assert (
        main(
            [
                "export",
                "--content-root",
                str(content_root),
                "--output-dir",
                str(output_root),
                "--target-engine",
                "generic",
            ]
        )
        == 0
    )
    body = _read_json(capsys)
    export_dir = output_root / "generic"
    assert body["output_dir"] == str(export_dir)
    assert (export_dir / "content_bundle.json").exists()
    assert (export_dir / "manifest.json").exists()
    assert body["manifest"]["target_engine"] == "generic"
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_export_unity_localization_optin(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    ContentStore(content_root).save(
        ContentBundle(
            localized_texts={
                "g_en": LocalizedText(id="g_en", text_key="greeting", locale="en", text="Hi"),
            }
        )
    )

    def _run(*extra: str) -> dict:
        assert (
            main(
                [
                    "export",
                    "--content-root",
                    str(content_root),
                    "--output-dir",
                    str(output_root),
                    "--target-engine",
                    "unity",
                    *extra,
                ]
            )
            == 0
        )
        return _read_json(capsys)

    # Without the flag: no localization projection.
    _run()
    assert not (output_root / "unity" / "unity" / "localization").exists()

    # With the flag: the official-format String Table CSV lands under unity/localization/.
    body = _run("--unity-localization")
    csv_path = output_root / "unity" / "unity" / "localization" / "owcopilot_strings.csv"
    assert csv_path.exists()
    assert csv_path.read_text(encoding="utf-8").splitlines()[0] == "Key,English(en)"
    kinds = {f["kind"] for f in body["manifest"]["files"]}
    assert {"unity_localization_csv", "unity_localization_readme"} <= kinds


def _write_clean_unreal_project(content_root) -> None:
    ContentStore(content_root).save(
        ContentBundle(
            entities={"npc_aldric": Entity(id="npc_aldric", name="Aldric", type=EntityType.NPC)},
            quests={
                "q1": Quest(
                    id="q1",
                    title="Q1",
                    giver_npc="npc_aldric",
                    objective="Help Aldric.",
                    localization_keys=["quest.q1.objective"],
                )
            },
        )
    )


def test_cli_export_row_struct_map_renames_columns(tmp_path, capsys) -> None:
    """A1 (CLI entry): --row-struct-map threads a rename file to the UE projection — the renamed
    column reaches Quests.json and the row header, proving the flag is wired end to end."""
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_clean_unreal_project(content_root)
    mapping = tmp_path / "row_struct_map.json"
    mapping.write_text(
        json.dumps({"quests": {"DisplayName": "sDisplayName"}}), encoding="utf-8"
    )

    assert (
        main(
            [
                "export",
                "--content-root",
                str(content_root),
                "--output-dir",
                str(output_root),
                "--target-engine",
                "unreal",
                "--row-struct-map",
                str(mapping),
            ]
        )
        == 0
    )
    unreal_dir = output_root / "unreal" / "unreal"
    quests = json.loads((unreal_dir / "Quests.json").read_text(encoding="utf-8"))
    row = next(r for r in quests if r["Name"] == "q1")
    assert "sDisplayName" in row and "DisplayName" not in row
    header = (unreal_dir / "OWCopilotRows.h").read_text(encoding="utf-8")
    assert "FText sDisplayName;" in header


def test_cli_export_row_struct_map_rejects_malformed_file(tmp_path, capsys) -> None:
    """A5 boundary: a row-struct-map file whose value is not a field->name object is refused with a
    guided error (exit 2 + JSON on stderr), not a raw traceback or a silent default-name export."""
    content_root = tmp_path / "content"
    output_root = tmp_path / "exports"
    _write_clean_unreal_project(content_root)
    mapping = tmp_path / "bad_map.json"
    mapping.write_text(json.dumps({"quests": "not-an-object"}), encoding="utf-8")

    exit_code = main(
        [
            "export",
            "--content-root",
            str(content_root),
            "--output-dir",
            str(output_root),
            "--target-engine",
            "unreal",
            "--row-struct-map",
            str(mapping),
        ]
    )
    assert exit_code == 2
    err = _read_stderr_json(capsys)
    assert err["type"] == "ValueError"
    assert "row-struct-map" in err["error"]
    # The malformed map must abort before any UE file is written (no silent default-name export).
    assert not (output_root / "unreal" / "unreal" / "Quests.json").exists()


def test_cli_ask_refuses_without_context(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())

    assert main(["ask", "--content-root", str(content_root), "--query", "Who is Aldric?"]) == 0
    body = _read_json(capsys)
    assert body["answer"]["refused"] is True
    assert body["telemetry"]["calls"] == 0
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_reports_invalid_content_root(tmp_path, capsys) -> None:
    missing_root = tmp_path / "missing"

    assert main(["audit", "--content-root", str(missing_root)]) == 2
    body = _read_stderr_json(capsys)
    assert body["type"] == "FileNotFoundError"
    assert "content root does not exist" in body["error"]


def test_cli_reports_missing_input_file(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    missing_source = tmp_path / "missing.json"

    assert (
        main(["ingest", "--content-root", str(content_root), "--input", str(missing_source)]) == 2
    )
    body = _read_stderr_json(capsys)
    assert body["type"] == "FileNotFoundError"


def test_cli_eval_golden_runs_offline_report(tmp_path, capsys) -> None:
    workspace = tmp_path / "golden_eval"
    output_path = tmp_path / "report.json"

    assert main(["eval-golden", "--workspace", str(workspace), "--output", str(output_path)]) == 0
    body = _read_json(capsys)
    assert body["passed"] is True
    assert output_path.exists()
    assert (workspace / "exports" / "generic" / "manifest.json").exists()


def test_cli_repair_plan_llm_flags_parse() -> None:
    # Defaults: LLM widening is off, the budget knob defaults to 32, and llm mode is offline.
    args = build_parser().parse_args(["repair-plan", "--content-root", "."])
    assert args.llm_candidates is False
    assert args.max_llm_calls == 32
    assert args.llm_mode == "offline"

    opted_in = build_parser().parse_args(
        [
            "repair-plan",
            "--content-root",
            ".",
            "--llm-candidates",
            "--max-llm-calls",
            "5",
            "--llm-mode",
            "real",
        ]
    )
    assert opted_in.llm_candidates is True
    assert opted_in.max_llm_calls == 5
    assert opted_in.llm_mode == "real"


def test_cli_repair_plan_deterministic_default(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )

    assert main(["repair-plan", "--content-root", str(content_root)]) == 0
    body = _read_json(capsys)
    assert body["used_llm"] is False
    assert body["llm_mode"] == "offline"
    assert body["resolved_errors"] >= 1  # the dangling giver_npc is deterministically fixable
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_repair_plan_llm_candidates_offline_is_noop(tmp_path, capsys) -> None:
    # There is no offline double for patch_suggest (mirrors `suggest`): opting into --llm-candidates
    # in offline mode stays on the deterministic $0 path rather than fabricating model output.
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(quests={"q1": Quest(id="q1", title="Q1", giver_npc="npc_missing")})
    )

    assert (
        main(
            [
                "repair-plan",
                "--content-root",
                str(content_root),
                "--llm-candidates",
                "--llm-mode",
                "offline",
            ]
        )
        == 0
    )
    body = _read_json(capsys)
    assert body["used_llm"] is False
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_quest_explore_reports_outcomes_and_budget(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(
        ContentBundle(
            quests={
                "q": Quest(
                    id="q",
                    title="Q",
                    stages=[QuestStage(id="s1", summary="a"), QuestStage(id="s2", summary="b")],
                    logic=QuestLogic(
                        branches=[
                            Branch(id="b_end", from_stage="s1", condition="true", outcome="done"),
                            Branch(id="b_go", from_stage="s1", to_stage="s2"),
                        ]
                    ),
                )
            }
        )
    )

    assert main(["quest-explore", "--content-root", str(content_root), "q"]) == 0
    body = _read_json(capsys)
    assert body["quest_id"] == "q"
    assert body["truncated"] is False
    assert "done" in {o["outcome"] for o in body["outcomes"] if o["reachable"]}
    assert body["cost_budget"]["used_usd"] == 0.0


def test_cli_quest_explore_missing_id_is_guided_error(tmp_path, capsys) -> None:
    content_root = tmp_path / "content"
    ContentStore(content_root).save(ContentBundle())

    assert main(["quest-explore", "--content-root", str(content_root), "ghost"]) == 2
    body = _read_stderr_json(capsys)
    assert body["type"] == "ValueError"
    assert "任务" in body["error"] and "不存在" in body["error"]


def test_output_encoding_survives_gbk_console_with_emoji(monkeypatch) -> None:
    """Windows GBK 控制台回归：LLM 输出带 emoji（GBK 编码外字符）时，CLI 输出必须降级转义
    而不是整条命令 UnicodeEncodeError 崩掉（曾以 exit 2 抛 raw error）。"""
    import io
    import sys

    from owcopilot.cli.main import _configure_output_encoding

    gbk_stdout = io.TextIOWrapper(io.BytesIO(), encoding="gbk", errors="strict")
    monkeypatch.setattr(sys, "stdout", gbk_stdout)

    _configure_output_encoding()

    text = '{"answer": "修复完成 ✔️ ️"}'  # 中文可编码；emoji/变体选择符在 GBK 之外
    print(text, file=sys.stdout)  # errors=strict 时这里会抛 UnicodeEncodeError
    sys.stdout.flush()
    raw = gbk_stdout.buffer.getvalue()
    assert "修复完成".encode("gbk") in raw  # 中文照常
    assert b"\ufe0f" in raw  # 编码外字符降级为转义而非崩溃
