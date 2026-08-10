# 外部导入：引擎回流 + 识别/应用外部工程文件 + 映射模板。
# Engine import + recognize/apply + mappings.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import (
    delete_mapping_template_action,
    import_from_engine_action,
    list_mapping_templates_action,
    recognize_apply_plan_action,
    recognize_content_action,
    save_mapping_template_action,
)
from ..dependencies import _llm_mode, _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import (
    EngineImportRequest,
    MappingTemplateRequest,
    RecognizeApplyRequest,
    RecognizeRequest,
)

router = APIRouter()


@router.post("/projects/{project}/engine:import")
def engine_import(
    project: str,
    req: EngineImportRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-K · pull engine-side quest rows back: diff vs canon, queue new/changed for review."""
    try:
        result = import_from_engine_action(
            _project_root_or_404(project),
            quests=req.quests,
            baseline_quests=req.baseline_quests,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/import:recognize")
def import_recognize(
    project: str,
    req: RecognizeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-R · recognize a foreign project file (table/articy/ink/yarn/ue/unity) into an editable
    plan; with apply=true, stage new/changed into review + return a pre-merge audit preview."""
    try:
        result = recognize_content_action(
            _project_root_or_404(project),
            source_format=req.source_format,
            content=req.content,
            content_base64=req.content_base64,
            filename=req.filename,
            field_mapping=req.field_mapping,
            apply=req.apply,
            enable_llm=req.enable_llm,
            llm_mode=_llm_mode(),
            operator=req.operator,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/import:apply")
def import_apply(
    project: str,
    req: RecognizeApplyRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-R · stage an edited ImportPlan (human kept/dropped proposals) into review."""
    try:
        result = recognize_apply_plan_action(
            _project_root_or_404(project), plan=req.plan, operator=req.operator
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.get("/projects/{project}/recognize/mappings")
def recognize_mappings_list(
    project: str,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1_000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = list_mapping_templates_action(_project_root_or_404(project))
    if limit is None and cursor is None:
        # Backward compatibility for CLI/older clients: the original no-query shape is the full
        # name -> ColumnMapping dictionary and remains byte-shape compatible.
        return {"project": project, **result}

    ordered = [
        {"name": name, "mapping": mapping} for name, mapping in sorted(result["templates"].items())
    ]
    try:
        page = paginate(ordered, limit=limit or 20, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "templates": {item["name"]: item["mapping"] for item in page.items},
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.post("/projects/{project}/recognize/mappings")
def recognize_mappings_save(
    project: str,
    req: MappingTemplateRequest,
    request: Request,
    include_details: bool = Query(default=True),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = save_mapping_template_action(
            _project_root_or_404(project), name=req.name, mapping=req.mapping
        )
    except ValueError as e:
        raise _manage_error(e) from e
    if not include_details:
        result = {key: value for key, value in result.items() if key != "templates"}
    return {"project": project, **result}


@router.delete("/projects/{project}/recognize/mappings/{name}")
def recognize_mappings_delete(
    project: str,
    name: str,
    request: Request,
    include_details: bool = Query(default=True),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = delete_mapping_template_action(_project_root_or_404(project), name=name)
    if not include_details:
        result = {key: value for key, value in result.items() if key != "templates"}
    return {"project": project, **result}
