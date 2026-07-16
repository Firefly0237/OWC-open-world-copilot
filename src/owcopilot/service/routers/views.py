# 只读投影：概览/存档/就绪度/时间线/关系图/对白/快照/检索等视图模型。
# Read-only view-model projections.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import search_all_action
from ...app.view_models import (
    build_content_inventory,
    build_dialogue_flow_view_model,
    build_dialogue_list_view_model,
    build_dialogue_tree_view_model,
    build_diff_view_model,
    build_graph_view_model,
    build_project_overview,
    build_quest_view_model,
    build_readiness_report,
    build_snapshots_view_model,
    build_timeline_view_model,
    create_world_snapshot,
    relation_kinds_view_model,
)
from ..dependencies import _project_root_or_404
from ..schemas import SnapshotCreateRequest

router = APIRouter()


@router.get("/projects/{project}/overview")
def project_overview(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return {
        "project": project,
        "overview": build_project_overview(_project_root_or_404(project)),
    }


@router.get("/projects/{project}/archive")
def project_archive(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return {
        "project": project,
        "inventory": build_content_inventory(_project_root_or_404(project)),
    }


@router.get("/projects/{project}/readiness")
def project_readiness(
    project: str,
    request: Request,
    kind: str | None = None,
    only_incomplete: bool = False,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Completeness scoring of every content item (field-completeness, not correctness)."""
    return {
        "project": project,
        "readiness": build_readiness_report(
            _project_root_or_404(project),
            only_incomplete=only_incomplete,
            kind=kind,
        ),
    }


@router.get("/projects/{project}/timeline")
def project_timeline(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Chronology of quests + events with timeline violations marked (read-only, $0)."""
    return {
        "project": project,
        "timeline": build_timeline_view_model(_project_root_or_404(project)),
    }


@router.get("/projects/{project}/graph")
def project_graph(
    project: str,
    request: Request,
    focus: str = "",
    radius: int = 1,
    kinds: str | None = None,
    impact: bool = False,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Relationship subgraph (read-only). With ``focus`` → ego graph + optional impact ripple;
    without ``focus`` → whole-world clustered overview."""
    kind_set = {k.strip() for k in (kinds or "").split(",") if k.strip()} or None
    return {
        "project": project,
        "graph": build_graph_view_model(
            _project_root_or_404(project),
            focus_ref=focus or None,
            radius=max(1, min(radius, 3)),
            kinds=kind_set,
            impact=impact,
        ),
    }


@router.get("/projects/{project}/relation_kinds")
def project_relation_kinds(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """The pre-provided relationship-kind catalog the editor offers (custom still allowed)."""
    return {"project": project, **relation_kinds_view_model()}


@router.get("/projects/{project}/dialogue_trees")
def project_dialogue_trees(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """List the world's branching dialogue trees (read-only, $0)."""
    return {
        "project": project,
        "dialogues": build_dialogue_list_view_model(_project_root_or_404(project)),
    }


@router.get("/projects/{project}/dialogue_trees/{tree_id}/flow")
def project_dialogue_flow(
    project: str,
    tree_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Laid-out flow graph for one dialogue tree (read-only, $0)."""
    flow = build_dialogue_flow_view_model(_project_root_or_404(project), tree_id=tree_id)
    if flow is None:
        raise HTTPException(status_code=404, detail=f"dialogue tree {tree_id!r} not found")
    return {"project": project, "flow": flow}


@router.get("/projects/{project}/dialogue_trees/{tree_id}")
def project_dialogue_tree(
    project: str,
    tree_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Full structural dialogue tree for the editor (untruncated text/choices, read-only $0)."""
    tree = build_dialogue_tree_view_model(_project_root_or_404(project), tree_id=tree_id)
    if tree is None:
        raise HTTPException(status_code=404, detail=f"dialogue tree {tree_id!r} not found")
    return {"project": project, "tree": tree}


@router.get("/projects/{project}/snapshots")
def project_snapshots(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """List canon snapshots, newest first (read-only, $0)."""
    return {"project": project, **build_snapshots_view_model(_project_root_or_404(project))}


@router.post("/projects/{project}/snapshots", status_code=201)
def create_project_snapshot(
    project: str,
    req: SnapshotCreateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Take a labelled snapshot of the current world (writes under .snapshots/)."""
    meta = create_world_snapshot(_project_root_or_404(project), label=req.label)
    return {"project": project, "snapshot": meta}


@router.get("/projects/{project}/diff")
def project_diff(
    project: str,
    request: Request,
    from_id: str = Query(alias="from"),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Structural diff of a snapshot against the current world (read-only, $0)."""
    diff = build_diff_view_model(_project_root_or_404(project), from_id=from_id)
    if diff is None:
        raise HTTPException(status_code=404, detail=f"snapshot {from_id!r} not found")
    return {"project": project, "diff": diff}


@router.get("/projects/{project}/quests/{quest_id}")
def get_quest(
    project: str,
    quest_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Full quest (objective/prereqs absent from the timeline payload) for the editor ($0)."""
    quest = build_quest_view_model(_project_root_or_404(project), quest_id=quest_id)
    if quest is None:
        raise HTTPException(status_code=404, detail=f"quest {quest_id!r} not found")
    return {"project": project, "quest": quest}


@router.get("/projects/{project}/search")
def search_project(
    project: str,
    request: Request,
    q: str,
    kinds: str | None = None,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Global literal search across the canon (jump-to). $0, deterministic."""
    kind_list = [k for k in (kinds or "").split(",") if k] or None
    result = search_all_action(_project_root_or_404(project), query=q, kinds=kind_list)
    return {"project": project, **result}
