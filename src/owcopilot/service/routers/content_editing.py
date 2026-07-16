# 人工直接编辑：实体/任务/风格/关系/对白/位置/改名/快照恢复（同人审写入管线）。
# Human direct-edit writes.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request

from ...app.actions import (
    add_relation_action,
    apply_rename_action,
    create_entity_action,
    delete_object_action,
    draft_quest_logic_action,
    plan_rename_action,
    remove_relation_action,
    restore_snapshot_action,
    set_object_position_action,
    simulate_quest_action,
    update_dialogue_tree_action,
    update_entity_action,
    update_quest_action,
    update_quest_logic_action,
    update_style_guide_action,
)
from ..dependencies import _manage_error, _project_root_or_404
from ..schemas import (
    DialogueTreePatchRequest,
    EntityCreateRequest,
    EntityUpdateRequest,
    EntityUpdateResponse,
    NodePositionRequest,
    ObjectDeleteResponse,
    QuestLogicDraftRequest,
    QuestLogicPatchRequest,
    QuestPatchRequest,
    QuestSimulateRequest,
    RelationRequest,
    RenameRequest,
    SnapshotRestoreRequest,
    StyleGuidePatchRequest,
)

router = APIRouter()


@router.patch("/projects/{project}/entities/{entity_id}", response_model=EntityUpdateResponse)
def update_entity(
    project: str,
    entity_id: str,
    req: EntityUpdateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> EntityUpdateResponse:
    try:
        result = update_entity_action(
            _project_root_or_404(project),
            entity_id=entity_id,
            name=req.name,
            description=req.description,
            tags=req.tags,
            metadata_updates=req.metadata_updates,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return EntityUpdateResponse(project=project, **result)


@router.patch("/projects/{project}/style_guides/{guide_id}")
def update_style_guide(
    project: str,
    guide_id: str,
    req: StyleGuidePatchRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """B10: inline-edit the worldview style guide (body + rules); lands at once (human edit)."""
    try:
        result = update_style_guide_action(
            _project_root_or_404(project),
            guide_id=guide_id,
            body=req.body,
            rules=req.rules,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.delete(
    "/projects/{project}/objects/{ref_type}/{object_id}",
    response_model=ObjectDeleteResponse,
)
def delete_object(
    project: str,
    ref_type: str,
    object_id: str,
    request: Request,
    cascade_relations: bool = True,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ObjectDeleteResponse:
    try:
        result = delete_object_action(
            _project_root_or_404(project),
            ref_type=ref_type,
            object_id=object_id,
            cascade_relations=cascade_relations,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return ObjectDeleteResponse(project=project, **result)


# ---- graph/timeline/dialogue direct editing (same human-edit pipeline as PATCH entity) ----


@router.post("/projects/{project}/entities", status_code=201)
def create_entity(
    project: str,
    req: EntityCreateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = create_entity_action(
            _project_root_or_404(project),
            name=req.name,
            entity_type=req.type,
            description=req.description,
            tags=req.tags,
            metadata=req.metadata,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.patch("/projects/{project}/quests/{quest_id}")
def patch_quest(
    project: str,
    quest_id: str,
    req: QuestPatchRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = update_quest_action(
            _project_root_or_404(project),
            quest_id=quest_id,
            title=req.title,
            objective=req.objective,
            timeline_order=req.timeline_order,
            set_timeline_order=req.set_timeline_order,
            prerequisites=req.prerequisites,
            giver_npc=req.giver_npc,
            location=req.location,
            if_match=req.if_match,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.patch("/projects/{project}/quests/{quest_id}/logic")
def patch_quest_logic(
    project: str,
    quest_id: str,
    req: QuestLogicPatchRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Set/replace a quest's native logic layer; returns the deterministic logic issues."""
    try:
        result = update_quest_logic_action(
            _project_root_or_404(project), quest_id=quest_id, logic=req.logic
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/quests/{quest_id}/logic:draft")
def draft_quest_logic_endpoint(
    project: str,
    quest_id: str,
    req: QuestLogicDraftRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """B7: AI-draft the quest's logic, gated by the deterministic audit, queued for review."""
    try:
        result = draft_quest_logic_action(
            _project_root_or_404(project),
            quest_id=quest_id,
            intent=req.intent,
            refine_rounds=req.refine_rounds,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model or "deepseek-v4-flash",
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/quests/{quest_id}:simulate")
def simulate_quest_endpoint(
    project: str,
    quest_id: str,
    req: QuestSimulateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """WS-E playtest: walk the quest's logic and report path + outcome (deterministic, $0)."""
    try:
        result = simulate_quest_action(
            _project_root_or_404(project),
            quest_id=quest_id,
            choices=req.choices,
            initial_state=req.initial_state,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/rename:plan")
def rename_plan(
    project: str,
    req: RenameRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Dry-run a rename — the reference edits + conflicts, mutating nothing."""
    try:
        result = plan_rename_action(
            _project_root_or_404(project),
            ref=req.ref,
            new_name=req.new_name,
            new_id=req.new_id,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/rename:apply")
def rename_apply(
    project: str,
    req: RenameRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Apply a rename atomically (snapshot undo point + re-audit); returns the undo snapshot."""
    try:
        result = apply_rename_action(
            _project_root_or_404(project),
            ref=req.ref,
            operator=req.operator,
            new_name=req.new_name,
            new_id=req.new_id,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/snapshots:restore")
def snapshots_restore(
    project: str,
    req: SnapshotRestoreRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Undo by restoring a snapshot (e.g. the undo point a rename returned)."""
    try:
        result = restore_snapshot_action(_project_root_or_404(project), snapshot_id=req.snapshot_id)
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/relations", status_code=201)
def add_relation(
    project: str,
    req: RelationRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = add_relation_action(
            _project_root_or_404(project),
            source=req.source,
            target=req.target,
            kind=req.kind,
            symmetric=req.symmetric,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.delete("/projects/{project}/relations")
def remove_relation(
    project: str,
    request: Request,
    source: str = Query(),
    target: str = Query(),
    kind: str = Query(),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = remove_relation_action(
            _project_root_or_404(project), source=source, target=target, kind=kind
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.patch("/projects/{project}/dialogue_trees/{tree_id}")
def patch_dialogue_tree(
    project: str,
    tree_id: str,
    req: DialogueTreePatchRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = update_dialogue_tree_action(
            _project_root_or_404(project),
            tree_id=tree_id,
            title=req.title,
            root_node=req.root_node,
            nodes=req.nodes,
            metadata_updates=req.metadata_updates,
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/graph/positions")
def set_node_position(
    project: str,
    req: NodePositionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = set_object_position_action(
            _project_root_or_404(project), ref=req.ref, x=req.x, y=req.y
        )
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}
