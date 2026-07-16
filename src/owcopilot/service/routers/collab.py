# 协作账本：分派/评论/加解锁 + 状态查询。
# Collaboration ledger: assign/comment/lock + state.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import assign_action, collab_state_action, comment_action, lock_action
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import AssignRequest, CommentRequest, LockRequest

router = APIRouter()


@router.get("/projects/{project}/collab")
def collab_state(
    project: str,
    request: Request,
    object_ref: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """The collaboration ledger (assignments/comments/locks), optionally scoped to an object."""
    result = collab_state_action(_project_root_or_404(project), object_ref=object_ref)
    return {"project": project, **result}


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
