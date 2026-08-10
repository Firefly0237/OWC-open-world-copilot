# 受管世界工作区：增删/导入/打包下载。
# Managed world workspaces: CRUD + import + pack.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import base64
import binascii
import urllib.parse

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from ...app.workspaces import (
    create_managed_world,
    delete_managed_world,
    export_world_zip,
    import_world_zip,
    list_managed_worlds,
    managed_world_path,
)
from ...trust.security import PathSecurityError

# _is_loopback stays in ``service.api`` (monkeypatch seam); reach it via the module object.
from .. import api
from ..pagination import PaginationCursorError, paginate
from ..schemas import (
    WorkspaceCreateRequest,
    WorkspaceImportRequest,
    WorkspaceInfo,
    WorkspaceListResponse,
)

router = APIRouter()


@router.get(
    "/workspaces",
    response_model=WorkspaceListResponse,
    response_model_exclude_unset=True,
)
def list_workspaces(
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1000),
    focus: str | None = Query(default=None, max_length=500),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> WorkspaceListResponse:
    rows = list_managed_worlds()
    if limit is None and cursor is None and focus is None:
        return WorkspaceListResponse(workspaces=[WorkspaceInfo(**row) for row in rows])

    # A bounded shell selector still needs to show the current world even when it is older than
    # the first page. Moving the exact focus to the front preserves a complete forward cursor walk
    # (unlike starting at the page containing it, which would make earlier worlds unreachable).
    if focus:
        focus_index = next(
            (index for index, row in enumerate(rows) if row["name"] == focus),
            None,
        )
        if focus_index is not None and focus_index > 0:
            focused = rows.pop(focus_index)
            rows.insert(0, focused)
    try:
        page = paginate(rows, limit=limit or 30, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return WorkspaceListResponse(
        workspaces=[WorkspaceInfo(**row) for row in page.items],
        total=page.total,
        next_cursor=page.next_cursor,
        snapshot_etag=page.snapshot_etag,
    )


@router.post("/workspaces", response_model=WorkspaceInfo, status_code=201)
def create_workspace(
    req: WorkspaceCreateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> WorkspaceInfo:
    try:
        created = create_managed_world(req.name)
    except ValueError as e:
        status = 409 if "已存在" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return WorkspaceInfo(name=created.name, path=str(created))


@router.post("/workspaces:import", response_model=WorkspaceInfo, status_code=201)
def import_workspace(
    req: WorkspaceImportRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> WorkspaceInfo:
    try:
        data = base64.b64decode(req.zip_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail="zip_base64 不是有效的 base64") from e
    try:
        imported = import_world_zip(data, req.name)
    except ValueError as e:
        # name collision is a conflict; anything else (slip, empty, not a world
        # pack) is a bad request
        status = 409 if "已存在" in str(e) else 400
        raise HTTPException(status_code=status, detail=str(e)) from e
    return WorkspaceInfo(name=imported.name, path=str(imported))


@router.get("/workspaces/{name}/pack")
def download_workspace_pack(
    name: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    try:
        target = managed_world_path(name)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    if not target.exists():
        raise HTTPException(status_code=404, detail=f"世界「{name}」不存在")
    # HTTP headers are latin-1: CJK names need the RFC 5987 filename* form, with a
    # plain ASCII fallback for ancient clients
    encoded = urllib.parse.quote(f"{name}-pack.zip")
    try:
        payload = export_world_zip(target)
    except PathSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return Response(
        content=payload,
        media_type="application/zip",
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"world-pack.zip\"; filename*=UTF-8''{encoded}"
            )
        },
    )


@router.delete("/workspaces/{name}")
def delete_workspace(
    name: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, str]:
    """Delete a managed world (destructive, irreversible). Loopback-only: a remote client must
    never be able to wipe whoever-owns-this-server's worlds."""
    if not api._is_loopback(request):
        raise HTTPException(status_code=403, detail="删除世界仅限本机操作")
    try:
        delete_managed_world(name)
    except ValueError as e:
        raise HTTPException(status_code=404 if "不存在" in str(e) else 400, detail=str(e)) from e
    # ``delete_managed_world`` validates the identifier before touching disk.  Return the exact
    # public identifier; invalid input must fail instead of being rewritten into another world.
    return {"deleted": name}
