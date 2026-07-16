# 模板库：内置档案库 + 按模板实例化（经审阅队列）。
# Template library + instantiate -> review.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import instantiate_template_action, list_templates_action
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import TemplateInstantiateRequest

router = APIRouter()


@router.get("/templates")
def templates_library(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-G · the built-in template / archetype library (project-independent)."""
    return list_templates_action()


@router.post("/projects/{project}/templates:instantiate", status_code=201)
def templates_instantiate(
    project: str,
    req: TemplateInstantiateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Stamp out content from a template + params, routed through the review queue."""
    try:
        result = instantiate_template_action(
            _project_root_or_404(project),
            template_id=req.template_id,
            params=req.params,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}
