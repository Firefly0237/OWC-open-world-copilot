# 协作账本：分派/评论/加解锁 + 状态查询。
# Collaboration ledger: assign/comment/lock + state.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import assign_action, collab_state_action, comment_action, lock_action
from ..dependencies import _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import AssignRequest, CommentRequest, LockRequest

router = APIRouter()


@router.get("/projects/{project}/collab")
def collab_state(
    project: str,
    request: Request,
    object_ref: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """The collaboration ledger (assignments/comments/locks), optionally scoped to an object."""
    result = collab_state_action(_project_root_or_404(project), object_ref=object_ref)
    # No pagination parameters deliberately preserves the original response for CLI and older
    # clients.  Browser detail views opt into a bounded, snapshot-bound thread for one object.
    if limit is None and cursor is None:
        return {"project": project, **result}
    if object_ref is None:
        raise HTTPException(
            status_code=422,
            detail="评论分页需要 object_ref，请指定一个协作对象。",
        )
    try:
        # A bounded first page must contain the newest discussion; otherwise a long-lived thread
        # would hide a freshly posted comment until every historical page had been loaded.
        page = paginate(list(reversed(result["comments"])), limit=limit or 20, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        **result,
        "comments": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.post("/projects/{project}/collab/assign")
def collab_assign(
    project: str,
    req: AssignRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = assign_action(
            _project_root_or_404(project),
            object_ref=req.object_ref,
            assignee=req.assignee,
            by=req.by,
            note=req.note,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/collab/comments", status_code=201)
def collab_comment(
    project: str,
    req: CommentRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = comment_action(
            _project_root_or_404(project),
            object_ref=req.object_ref,
            author=req.author,
            body=req.body,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/collab/lock")
def collab_lock(
    project: str,
    req: LockRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Acquire or release an edit lock; another holder's lock blocks (409)."""
    try:
        result = lock_action(
            _project_root_or_404(project),
            object_ref=req.object_ref,
            holder=req.holder,
            release=req.release,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}
