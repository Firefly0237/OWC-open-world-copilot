# 本地化：覆盖度总览 + 状态流转/分派。
# Localization coverage + transition/assign.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import loc_assign_action, loc_transition_action, localization_overview_action
from ..dependencies import _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import LocAssignRequest, LocTransitionRequest

router = APIRouter()


@router.get("/projects/{project}/localization")
def localization_overview(
    project: str,
    request: Request,
    locales: str | None = None,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    focus: str | None = Query(default=None, max_length=500),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-F · localization coverage + per-string status. $0."""
    loc_list = [loc for loc in (locales or "").split(",") if loc] or None
    result = localization_overview_action(_project_root_or_404(project), locales=loc_list)
    # Old callers intentionally keep the complete overview when they do not request pagination.
    if limit is None and cursor is None:
        return {"project": project, **result}

    overview = dict(result["overview"])
    rows = list(overview["rows"])
    if focus:
        # Deep links must remain reachable after bounding the first page.  Moving one exact key to
        # the front is deterministic and the focus value is repeated for every cursor request.
        rows.sort(key=lambda row: (row.get("text_key") != focus, str(row.get("text_key", ""))))
    try:
        page = paginate(rows, limit=limit or 50, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    overview["rows"] = page.items
    return {
        "project": project,
        **result,
        "overview": overview,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.post("/projects/{project}/localization:transition")
def localization_transition(
    project: str,
    req: LocTransitionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = loc_transition_action(
            _project_root_or_404(project),
            text_key=req.text_key,
            locale=req.locale,
            to=req.to,
            by=req.by,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/localization:assign")
def localization_assign(
    project: str,
    req: LocAssignRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = loc_assign_action(
            _project_root_or_404(project),
            text_key=req.text_key,
            locale=req.locale,
            assignee=req.assignee,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}
