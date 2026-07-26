# 世界分析：确定性计数/密度/缺口/覆盖仪表盘。
# Deterministic world-analytics dashboard action.

"""analytics actions — split from the former ``app/actions.py`` monolith by domain.

Behaviour is unchanged; only the module boundary is new. The ``app.actions`` package
re-exports every action so existing imports keep working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._common import deterministic_cost_budget as _deterministic_cost_budget
from .._common import open_project as _project


def world_analytics_action(
    content_root: str | Path,
    *,
    sqlite_path: str | None = None,
) -> dict[str, Any]:
    """WS-H · deterministic world analytics dashboard (counts/density/gaps/coverage). $0."""
    from ..analytics import build_world_analytics

    with _project(content_root, sqlite_path) as project:
        return {
            "analytics": build_world_analytics(project.bundle),
            "cost_budget": _deterministic_cost_budget("world_analytics"),
        }
