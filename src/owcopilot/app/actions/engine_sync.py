# 引擎往返与外部导入：回流/识别/应用/映射模板/严格表格导入。
# Engine round-trip + foreign-project recognize/apply + mapping templates + table ingest.

"""engine_sync actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ...assist.review_queue import ReviewQueue
from ...audit.context import AuditContext
from ...pipeline.ingest import run_ingest
from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project
from ._shared import _gateway, _merge_preview


def run_ingest_action(
    content_root: str | Path,
    *,
    paths: list[str],
    sqlite_path: str | None = None,
    dry_run: bool = True,
    write_non_conflicting: bool = False,
) -> dict[str, Any]:
    """Strict-format import (xlsx/json/jsonl/md/luban). Defaults to dry-run preview."""
    with _project(content_root, sqlite_path) as project:
        result = run_ingest(
            project,
            list(paths),
            dry_run=dry_run,
            write_non_conflicting=write_non_conflicting,
        )
        return {
            "dry_run": result.dry_run,
            "incoming_count": result.incoming_count,
            "changes": [change.model_dump(mode="json") for change in result.changes],
            "issues": [issue.model_dump(mode="json") for issue in result.issues],
            "has_errors": result.has_errors,
            "content_hash_before": result.content_hash_before,
            "content_hash_after": result.content_hash_after,
            "cost_budget": _deterministic_cost_budget("ingest"),
        }


def import_from_engine_action(
    content_root: str | Path,
    *,
    quests: list[dict[str, Any]],
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-K · pull engine-side quest rows back: diff vs canon, queue new/changed for review."""
    from ..engine_sync import plan_engine_import, staged_bundle

    with _project(content_root, sqlite_path) as project:
        plan = plan_engine_import(quests, project.bundle)
        review_item_id = None
        staged = staged_bundle(plan)
        if staged.quests:
            item = ReviewQueue(project.sqlite_store).add_world_seed(
                {"id": "engine_import", "bundle": staged.model_dump(mode="json")}
            )
            review_item_id = item.id
        return {
            "new": plan["new"],
            "changed": plan["changed"],
            "unchanged": plan["unchanged"],
            "review_item_id": review_item_id,
            "cost_budget": _deterministic_cost_budget("engine_import"),
        }


# >>> WS-R recognize foreign project files (tables / articy) → editable plan → review >>>
def _read_table_rows(input_path: str | Path) -> list[dict[str, Any]]:
    """Read a spreadsheet into raw header-keyed rows, reusing the existing tolerant importers
    (GB18030/Chinese-header CSV, openpyxl XLSX, JSON array). Headers are kept verbatim so the
    column-mapping step sees exactly what the planner wrote."""
    path = Path(input_path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        from ...content.importers.csv import CSVImporter

        return [dict(obj.data) for obj in CSVImporter().parse(path)]
    if suffix in {".xlsx", ".xlsm"}:
        from ...content.importers.xlsx import XLSXImporter

        return [dict(obj.data) for obj in XLSXImporter().parse(path)]
    if suffix == ".json":
        import json

        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return [dict(row) for row in data if isinstance(row, dict)]
        raise ValueError("表格 JSON 需为对象数组（list[dict]）")
    raise ValueError(f"不支持的表格文件类型：{suffix or path.name}（支持 .csv/.xlsx/.json）")


_RECOGNIZE_FORMATS = ("table", "articy", "ink", "yarn", "ue", "unity")


def _table_rows_from_text(text: str) -> list[dict[str, Any]]:
    """Parse pasted/uploaded table content: a JSON array of objects, or CSV text."""
    import csv
    import io
    import json

    if text.lstrip().startswith("["):
        data = json.loads(text)
        return [dict(row) for row in data if isinstance(row, dict)]
    reader = csv.DictReader(io.StringIO(text))
    return [{str(k): (v if v is not None else "") for k, v in row.items() if k} for row in reader]


def _recognize_plan(
    project: Any,
    source_format: str,
    *,
    source_name: str,
    rows: list[dict[str, Any]] | None = None,
    text: str | None = None,
    data: Any = None,
    field_mapping: dict[str, Any] | None = None,
    llm_proposer: Any = None,
) -> Any:
    """Build the editable ImportPlan from an already-loaded payload (shared by path + content)."""
    from ...recognize import ColumnMapping, recognize

    canon_ids = list(project.bundle.entities.keys())
    common = {
        "source_file": source_name,
        "enable_llm": llm_proposer is not None,
        "llm_proposer": llm_proposer,
    }
    if source_format == "table":
        mapping = ColumnMapping.model_validate(field_mapping) if field_mapping else None
        return recognize("table", rows=rows or [], mapping=mapping, canon_ids=canon_ids, **common)
    if source_format == "articy":
        return recognize("articy", articy_data=data, **common)
    if source_format in {"ink", "yarn"}:
        return recognize(source_format, text=text or "", llm_text=text or "", **common)
    if source_format in {"ue", "unity"}:
        return recognize(source_format, engine_data=data, canon_ids=canon_ids, **common)
    raise ValueError(
        f"暂不支持的来源格式：{source_format}（支持：{', '.join(_RECOGNIZE_FORMATS)}）"
    )


def _recognize_finish(project: Any, plan: Any, *, apply: bool) -> dict[str, Any]:
    """Diff vs canon, build the response, and (on apply) stage new/changed + audit preview."""
    from ...recognize import diff_against_canon, plan_to_bundle

    plan = diff_against_canon(plan, project.bundle)
    result: dict[str, Any] = {
        "source_format": plan.source_format,
        "summary": plan.summary(),
        "plan": plan.model_dump(mode="json"),
        "new": plan.new,
        "changed": plan.changed,
        "unchanged": plan.unchanged,
        "warnings": plan.warnings,
        "applied": False,
        "cost_budget": _deterministic_cost_budget("recognize_import"),
    }
    if not apply:
        return result

    staged = plan_to_bundle(plan, only_ids=set(plan.new) | set(plan.changed))
    review_item_id = None
    if staged.entities or staged.relations:
        item = ReviewQueue(project.sqlite_store).add_world_seed(
            {"id": "recognition_import", "bundle": staged.model_dump(mode="json")}
        )
        review_item_id = item.id
    preview = project.audit_runner.run(
        AuditContext.from_bundle(_merge_preview(project.bundle, staged))
    )
    result.update(
        {
            "applied": True,
            "review_item_id": review_item_id,
            "audit_preview": {
                "totals": preview.run.totals,
                "issues": [
                    {
                        "rule_code": issue.rule_code,
                        "severity": issue.severity.value,
                        "target_ref": issue.target_ref,
                        "message": issue.message,
                    }
                    for issue in preview.issues[:20]
                ],
            },
        }
    )
    return result


def sync_back_action(
    content_root: str | Path,
    *,
    input_path: str,
    baseline_path: str | None = None,
    apply: bool = False,
    explain: bool = False,
    gateway: Any = None,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """E3 · controlled engine back-flow: engine-edited quest rows -> three-way diff vs canon and
    the export baseline -> human review queue. Conflicts (both sides moved since export) are never
    staged; nothing lands in the library without an approved review — zero authoritative writes."""
    import json

    from ...content.encoding import decode_bytes
    from ..engine_sync import plan_engine_sync_back, staged_bundle

    def _rows(path: str) -> list[dict[str, Any]]:
        data = json.loads(decode_bytes(Path(path).read_bytes()))
        if not isinstance(data, list):
            raise ValueError(
                f"引擎数据文件应是任务行数组（v2 形状的 JSON array，桥接工具交换的格式）：{path}"
            )
        return data

    incoming = _rows(input_path)
    baseline = _rows(baseline_path) if baseline_path else None
    with _project(content_root, sqlite_path) as project:
        try:
            plan = plan_engine_sync_back(incoming, project.bundle, baseline=baseline)
        except ValidationError as exc:
            raise ValueError(
                "行数据不符合 v2 任务形状（需要 id/title 等字段）。若这是 UE DataTable 原始导出"
                "（Name/DisplayName 键），请改用 `owcopilot recognize --source-format ue`；"
                f"sync-back 只接受引擎桥交换的 v2 行。原始错误：{exc.errors()[:2]}"
            ) from exc
        result: dict[str, Any] = {
            key: plan[key]
            for key in (
                "new",
                "changed",
                "unchanged",
                "conflicts",
                "library_ahead",
                "engine_missing",
                "baseline_provided",
                "warnings",
            )
        }
        result["applied"] = False
        result["cost_budget"] = _deterministic_cost_budget("engine_sync_back")
        if explain and (plan["conflicts"] or plan["library_ahead"] or plan["warnings"]):
            from ...assist.engine_explain import explain_engine_issues, issues_from_warnings

            issues = [
                {"code": "sync_conflict", "target": qid, "message": ""} for qid in plan["conflicts"]
            ]
            issues += [
                {"code": "library_ahead", "target": qid, "message": ""}
                for qid in plan["library_ahead"]
            ]
            issues += issues_from_warnings(plan["warnings"], code="recognize_warning")
            result["explained"] = explain_engine_issues(
                issues, known_ids=set(project.bundle.quests), gateway=gateway
            ).model_dump(mode="json")
        if not apply:
            return result
        staged = staged_bundle(plan)
        review_item_id = None
        if staged.quests:
            item = ReviewQueue(project.sqlite_store).add_world_seed(
                {"id": "engine_sync_back", "bundle": staged.model_dump(mode="json")}
            )
            review_item_id = item.id
        preview = project.audit_runner.run(
            AuditContext.from_bundle(_merge_preview(project.bundle, staged))
        )
        result.update(
            {
                "applied": True,
                "review_item_id": review_item_id,
                "audit_preview": {"totals": preview.run.totals},
            }
        )
        return result


def recognize_import_action(
    content_root: str | Path,
    *,
    source_format: str,
    input_path: str,
    field_mapping: dict[str, Any] | None = None,
    apply: bool = False,
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · read a foreign project FILE on disk and recognize it into an editable ImportPlan
    (see ``_recognize_finish``). Recognition never auto-lands; new/changed go to human review."""
    import json

    from ...content.encoding import decode_bytes

    with _project(content_root, sqlite_path) as project:
        source_name = Path(input_path).name
        rows = _read_table_rows(input_path) if source_format == "table" else None
        # Use decode_bytes (not read_text(encoding="utf-8")) so GB18030 / UTF-16-BOM files
        # work correctly on Chinese Windows machines — fixes the P2 hardcoded-utf-8 bug.
        text = (
            decode_bytes(Path(input_path).read_bytes())
            if source_format in {"ink", "yarn"}
            else None
        )
        data = (
            json.loads(decode_bytes(Path(input_path).read_bytes()))
            if source_format in {"articy", "ue", "unity"}
            else None
        )
        plan = _recognize_plan(
            project,
            source_format,
            source_name=source_name,
            rows=rows,
            text=text,
            data=data,
            field_mapping=field_mapping,
        )
        return _recognize_finish(project, plan, apply=apply)


def _rows_from_upload(raw: bytes, filename: str) -> list[dict[str, Any]]:
    """Parse uploaded table bytes (CSV/XLSX/JSON) by writing a temp file with the right suffix and
    reusing the encoding-tolerant importers — so a GB18030 CSV or binary .xlsx parses correctly."""
    import tempfile

    suffix = Path(filename).suffix.lower() or ".csv"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(raw)
        tmp = handle.name
    try:
        return _read_table_rows(tmp)
    finally:
        Path(tmp).unlink(missing_ok=True)


def recognize_content_action(
    content_root: str | Path,
    *,
    source_format: str,
    content: str | None = None,
    content_base64: str | None = None,
    filename: str = "upload",
    field_mapping: dict[str, Any] | None = None,
    apply: bool = False,
    enable_llm: bool = False,
    llm_mode: str = "real",
    llm_model: str = "deepseek-v4-flash",
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · recognize from inline content (the REST/frontend path; server reads no local path).

    ``content`` is decoded text (pasted) or ``content_base64`` is raw file bytes (uploaded — needed
    for binary .xlsx and for non-UTF-8 CSV). ``source_format='auto'`` sniffs the format from the
    filename + a content sample (always overridable). ``enable_llm`` turns on the §8-guarded
    LLM relation pass (default off; needs a connected model under ``llm_mode='real'``)."""
    import base64
    import json

    from ...content.encoding import decode_bytes
    from ...recognize import (
        OfflineRelationProvider,
        build_llm_relation_proposer,
        sniff_source_format,
    )

    raw = base64.b64decode(content_base64) if content_base64 else None
    text_all = content if content is not None else (decode_bytes(raw) if raw is not None else "")
    fmt = source_format
    if fmt == "auto":
        fmt = sniff_source_format(filename, text_all[:4096])

    with _project(content_root, sqlite_path) as project:
        # Built inside the project block so the gateway cache is scoped to this project (the
        # namespace contextvar is only set within `_project`).
        llm_proposer = None
        if enable_llm:
            gateway, _ = _gateway(
                task="recognize_relations",
                llm_mode=llm_mode,
                llm_model=llm_model,
                offline_provider=OfflineRelationProvider(),
            )
            llm_proposer = build_llm_relation_proposer(gateway)

        rows: list[dict[str, Any]] | None = None
        text: str | None = None
        data: Any = None
        if fmt == "table":
            rows = (
                _rows_from_upload(raw, filename)
                if raw is not None
                else _table_rows_from_text(text_all)
            )
        elif fmt in {"ink", "yarn"}:
            text = text_all
        elif fmt in {"articy", "ue", "unity"}:
            data = json.loads(text_all)
        plan = _recognize_plan(
            project,
            fmt,
            source_name=filename,
            rows=rows,
            text=text,
            data=data,
            field_mapping=field_mapping,
            llm_proposer=llm_proposer,
        )
        return _recognize_finish(project, plan, apply=apply)


def recognize_apply_plan_action(
    content_root: str | Path,
    *,
    plan: dict[str, Any],
    operator: str = "import",
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-R · stage an EDITED ImportPlan into review. The UI sends back the plan after the human has
    dropped wrong proposals / fixed fields, so exactly the kept entities + relations get queued."""
    from ...recognize import ImportPlan

    with _project(content_root, sqlite_path) as project:
        parsed = ImportPlan.model_validate(plan)
        return _recognize_finish(project, parsed, apply=True)


def _mapping_store_path(content_root: str | Path) -> Path:
    return Path(content_root) / ".owcopilot" / "recognize_mappings.json"


def _read_mappings(content_root: str | Path) -> dict[str, Any]:
    import json

    path = _mapping_store_path(content_root)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _write_mappings(content_root: str | Path, data: dict[str, Any]) -> None:
    import json

    path = _mapping_store_path(content_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def list_mapping_templates_action(
    content_root: str | Path, *, sqlite_path: str | None = None
) -> dict[str, Any]:
    """Project-local saved column mappings (same-studio table schemas are stable, so reuse pays)."""
    return {"templates": _read_mappings(content_root)}


def save_mapping_template_action(
    content_root: str | Path, *, name: str, mapping: dict[str, Any], sqlite_path: str | None = None
) -> dict[str, Any]:
    from ...recognize import ColumnMapping

    if not name.strip():
        raise ValueError("模板名不能为空")
    validated = ColumnMapping.model_validate(mapping).model_dump(mode="json")
    data = _read_mappings(content_root)
    data[name] = validated
    _write_mappings(content_root, data)
    return {"templates": data, "saved": name}


def delete_mapping_template_action(
    content_root: str | Path, *, name: str, sqlite_path: str | None = None
) -> dict[str, Any]:
    data = _read_mappings(content_root)
    data.pop(name, None)
    _write_mappings(content_root, data)
    return {"templates": data, "deleted": name}
