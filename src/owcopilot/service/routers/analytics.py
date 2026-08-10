# 世界分析仪表盘：确定性计数/密度/缺口/覆盖。
# Deterministic world-analytics dashboard.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import world_analytics_action
from ..dependencies import _project_root_or_404
from ..pagination import PaginationCursorError, paginate

router = APIRouter()


@router.get("/projects/{project}/analytics")
def world_analytics(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-H deterministic world analytics dashboard (counts/density/gaps/coverage). $0."""
    result = world_analytics_action(_project_root_or_404(project))
    return {"project": project, **result}


@router.get("/projects/{project}/analytics/summary")
def world_analytics_summary(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Bounded dashboard summary; growing faction/gap rows live behind cursor endpoints."""

    result = world_analytics_action(_project_root_or_404(project))
    analytics = dict(result["analytics"])
    analytics.pop("factions", None)
    analytics.pop("underdeveloped_factions", None)
    analytics.pop("gaps", None)
    return {"project": project, **result, "analytics": analytics}


@router.get("/projects/{project}/analytics/factions")
def world_analytics_factions(
    project: str,
    request: Request,
    underdeveloped: bool = False,
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = world_analytics_action(_project_root_or_404(project))
    key = "underdeveloped_factions" if underdeveloped else "factions"
    rows = result["analytics"][key]
    try:
        page = paginate(rows, limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "underdeveloped": underdeveloped,
        "factions": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
        "cost_budget": result["cost_budget"],
    }


@router.get("/projects/{project}/analytics/gaps/{gap_kind}")
def world_analytics_gap_ids(
    project: str,
    gap_kind: str,
    request: Request,
    limit: int = Query(default=30, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = world_analytics_action(_project_root_or_404(project))
    gaps = result["analytics"]["gaps"]
    if gap_kind not in gaps:
        raise HTTPException(status_code=404, detail=f"未知的内容缺口类型：{gap_kind}")
    try:
        page = paginate(gaps[gap_kind], limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "gap_kind": gap_kind,
        "ids": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
        "cost_budget": result["cost_budget"],
    }
