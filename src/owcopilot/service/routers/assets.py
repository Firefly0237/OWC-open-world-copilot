# 媒体资产引用：列出/挂接/摘除。
# Media asset references: list/attach/detach.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import asset_attach_action, asset_detach_action, asset_list_action
from ..dependencies import _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import AssetAttachRequest

router = APIRouter()


@router.get("/projects/{project}/assets")
def assets_list(
    project: str,
    request: Request,
    object_ref: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-I · list media references attached to objects (or one object). $0."""
    result = asset_list_action(_project_root_or_404(project), object_ref=object_ref)
    # Preserve the original full ledger response for CLI/older clients. Object detail views opt
    # into a newest-first, snapshot-bound page so one long-lived asset thread cannot grow the DOM
    # or response without bound.
    if limit is None and cursor is None:
        return {"project": project, **result}
    if object_ref is None:
        raise HTTPException(
            status_code=422,
            detail="素材分页需要 object_ref，请指定一个正典对象。",
        )
    try:
        page = paginate(list(reversed(result["assets"])), limit=limit or 20, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        **result,
        "assets": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


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
