# 媒体资产引用：列出/挂接/摘除。
# Media asset references: list/attach/detach.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request

from ...app.actions import asset_attach_action, asset_detach_action, asset_list_action
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import AssetAttachRequest

router = APIRouter()


@router.get("/projects/{project}/assets")
def assets_list(
    project: str,
    request: Request,
    object_ref: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-I · list media references attached to objects (or one object). $0."""
    result = asset_list_action(_project_root_or_404(project), object_ref=object_ref)
    return {"project": project, **result}


@router.post("/projects/{project}/assets:attach", status_code=201)
def assets_attach(
    project: str,
    req: AssetAttachRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Attach an existing media reference (uri) to an object; idempotent per ref|kind|uri."""
    try:
        result = asset_attach_action(
            _project_root_or_404(project),
            object_ref=req.object_ref,
            kind=req.kind,
            uri=req.uri,
            title=req.title,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/assets:detach")
def assets_detach(
    project: str,
    request: Request,
    asset_id: str = Query(min_length=1),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = asset_detach_action(_project_root_or_404(project), asset_id=asset_id)
    return {"project": project, **result}
