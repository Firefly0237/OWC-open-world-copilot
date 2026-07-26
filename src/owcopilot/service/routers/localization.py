# 本地化：覆盖度总览 + 状态流转/分派。
# Localization coverage + transition/assign.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import loc_assign_action, loc_transition_action, localization_overview_action
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import LocAssignRequest, LocTransitionRequest

router = APIRouter()


@router.get("/projects/{project}/localization")
def localization_overview(
    project: str,
    request: Request,
    locales: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-F · localization coverage + per-string status. $0."""
    loc_list = [loc for loc in (locales or "").split(",") if loc] or None
    result = localization_overview_action(_project_root_or_404(project), locales=loc_list)
    return {"project": project, **result}


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
