# 世界分析仪表盘：确定性计数/密度/缺口/覆盖。
# Deterministic world-analytics dashboard.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import world_analytics_action
from ..dependencies import _project_root_or_404

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
