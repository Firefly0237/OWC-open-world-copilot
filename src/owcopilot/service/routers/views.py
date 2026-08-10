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
from ..pagination import PaginationCursorError, paginate
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
    kind: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=500),
    focus: str | None = Query(default=None, max_length=500),
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    inventory = build_content_inventory(_project_root_or_404(project))
    if kind is None and q is None and focus is None and limit is None and cursor is None:
        return {"project": project, "inventory": inventory}

    collection_keys = {
        "entity": "entities",
        "quest": "quests",
        "region": "regions",
        "poi": "pois",
        "term": "terms",
        "dialogue": "dialogues",
        "dialogue_tree": "dialogue_trees",
        "relation": "relations",
        "style_guide": "style_guides",
        "quest_event_ref": "quest_event_refs",
        "style_rubric": "style_rubrics",
        # Overview's node drill-down needs a bounded window too.  The dedicated graph page keeps
        # using the legacy no-parameter archive shape because rendering that view requires the
        # complete graph.
        "graph_ref": "graph_refs",
    }
    selected_kind = kind or "entity"
    collection_key = collection_keys.get(selected_kind)
    if collection_key is None:
        supported = ", ".join(collection_keys)
        raise HTTPException(
            status_code=422,
            detail=f"未知档案种类 {selected_kind!r}；可选值：{supported}",
        )

    counts = {name: len(inventory[key]) for name, key in collection_keys.items()}
    rows = list(inventory[collection_key])
    needle = (q or "").strip().casefold()
    if needle:
        import json

        rows = [
            row
            for row in rows
            if needle in json.dumps(row, ensure_ascii=False, sort_keys=True).casefold()
        ]

    cursor_rows = [{"kind": selected_kind, "item": row} for row in rows]
    page_limit = limit or 20
    start_offset = 0
    if focus and not cursor:
        focus_id = focus.split(":", 1)[-1]
        for index, row in enumerate(rows):
            if _archive_row_matches_focus(row, focus_id):
                start_offset = (index // page_limit) * page_limit
                break
    try:
        page = paginate(
            cursor_rows,
            limit=page_limit,
            cursor=cursor,
            start_offset=start_offset,
        )
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc

    items = [wrapped["item"] for wrapped in page.items]
    return {
        "project": project,
        "kind": selected_kind,
        "items": items,
        "counts": counts,
        "total": page.total,
        "limit": page_limit,
        "offset": page.offset,
        "page_cursor": page.page_cursor,
        "previous_cursor": page.previous_cursor,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
        "dialogue_tree_payloads": _page_dialogue_payloads(
            inventory,
            kind=selected_kind,
            items=items,
        ),
        "cost_budget": inventory["cost_budget"],
    }


def _archive_row_matches_focus(row: Any, focus_id: str) -> bool:
    if not isinstance(row, dict):
        return str(row) == focus_id
    if str(row.get("id", "")) == focus_id:
        return True
    relation_ref = f"{row.get('source', '')}:{row.get('kind', '')}:{row.get('target', '')}"
    return relation_ref == focus_id


def _page_dialogue_payloads(
    inventory: dict[str, Any],
    *,
    kind: str,
    items: list[Any],
) -> dict[str, Any]:
    """Return only tree payloads needed to render or deep-link the current page."""

    payloads = inventory.get("dialogue_tree_payloads", {})
    if kind == "dialogue_tree":
        tree_ids = {str(row.get("id", "")) for row in items}
        return {tree_id: payload for tree_id, payload in payloads.items() if tree_id in tree_ids}
    if kind != "dialogue":
        return {}

    dialogue_ids = {str(row.get("id", "")) for row in items}
    text_keys = {str(row.get("text_key", "")) for row in items if row.get("text_key")}
    selected: dict[str, Any] = {}
    for tree_id, payload in payloads.items():
        nodes = payload.get("nodes", {}) if isinstance(payload, dict) else {}
        if any(
            str(node_id) in dialogue_ids
            or str(node.get("id", "")) in dialogue_ids
            or str(node.get("text_key", "")) in text_keys
            for node_id, node in nodes.items()
            if isinstance(node, dict)
        ):
            selected[tree_id] = payload
    return selected


@router.get("/projects/{project}/readiness")
def project_readiness(
    project: str,
    request: Request,
    kind: str | None = None,
    only_incomplete: bool = False,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Completeness scoring of every content item (field-completeness, not correctness)."""
    readiness = build_readiness_report(
        _project_root_or_404(project),
        only_incomplete=only_incomplete,
        kind=kind,
    )
    # Keep the long-standing response byte-shape for callers that do not opt into paging.  The
    # summary stays project-wide when paging is enabled; only the potentially large ``items``
    # drill-down is windowed.
    if limit is None and cursor is None:
        return {"project": project, "readiness": readiness}

    page_limit = limit or 50
    try:
        page = paginate(readiness["items"], limit=page_limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    readiness["items"] = page.items
    return {
        "project": project,
        "readiness": readiness,
        "total": page.total,
        "limit": page_limit,
        "offset": page.offset,
        "page_cursor": page.page_cursor,
        "previous_cursor": page.previous_cursor,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
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
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    focus: str | None = Query(default=None, max_length=500),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """List the world's branching dialogue trees (read-only, $0)."""
    dialogues = build_dialogue_list_view_model(_project_root_or_404(project))
    if limit is None and cursor is None:
        return {"project": project, "dialogues": dialogues}
    trees = list(dialogues["trees"])
    if focus:
        trees.sort(key=lambda tree: (tree.get("id") != focus, str(tree.get("id", ""))))
    try:
        page = paginate(trees, limit=limit or 50, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "dialogues": {**dialogues, "trees": page.items},
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
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
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """List canon snapshots, newest first (read-only, $0)."""
    result = build_snapshots_view_model(_project_root_or_404(project))
    if limit is None and cursor is None:
        return {"project": project, **result}
    try:
        page = paginate(result["snapshots"], limit=limit or 50, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "snapshots": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


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
