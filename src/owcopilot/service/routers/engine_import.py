# 外部导入：引擎回流 + 识别/应用外部工程文件 + 映射模板。
# Engine import + recognize/apply + mappings.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import (
    delete_mapping_template_action,
    import_from_engine_action,
    list_mapping_templates_action,
    recognize_apply_plan_action,
    recognize_content_action,
    save_mapping_template_action,
)
from ..dependencies import _llm_mode, _manage_error, _project_root_or_404
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
        result = import_from_engine_action(_project_root_or_404(project), quests=req.quests)
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
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return {"project": project, **list_mapping_templates_action(_project_root_or_404(project))}


@router.post("/projects/{project}/recognize/mappings")
def recognize_mappings_save(
    project: str,
    req: MappingTemplateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = save_mapping_template_action(
            _project_root_or_404(project), name=req.name, mapping=req.mapping
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.delete("/projects/{project}/recognize/mappings/{name}")
def recognize_mappings_delete(
    project: str,
    name: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = delete_mapping_template_action(_project_root_or_404(project), name=name)
    return {"project": project, **result}
