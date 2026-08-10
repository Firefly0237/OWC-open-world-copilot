# 工程实验室：叙事模型检查、确定性修复搜索、风格清单与 Agent 运行史。
"""Canon-read-only engineering diagnostics exposed through typed FastAPI contracts."""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import (
    dialogue_walk_action,
    engineering_catalog_action,
    flag_xref_action,
    get_agent_run_action,
    list_agent_runs_action,
    quality_harness_action,
    quest_explore_action,
    repair_plan_action,
    style_sweep_action,
    submit_temporal_spec_action,
    translate_temporal_spec_action,
)
from ..api import _require_real_allowed
from ..dependencies import _manage_error, _project_root_or_404, _project_sqlite_path
from ..pagination import PaginationCursorError, paginate, paginate_window
from ..schemas import (
    AgentRunDetailResponse,
    AgentRunsResponse,
    AgentRunSummary,
    DialogueWalkRequest,
    DialogueWalkResponse,
    EngineeringCatalogResponse,
    FlagXrefResponse,
    QualityHarnessRequest,
    QualityHarnessResponse,
    QuestExploreRequest,
    QuestExploreResponse,
    RepairPlanRequest,
    RepairPlanResponse,
    StyleSweepRequest,
    StyleSweepResponse,
    TemporalSpecSubmitRequest,
    TemporalSpecSubmitResponse,
    TemporalSpecTranslateRequest,
    TemporalSpecTranslateResponse,
)

router = APIRouter(prefix="/projects/{project}/engineering", tags=["engineering"])


def _project_paths(project: str) -> tuple[str, str]:
    content_root = _project_root_or_404(project)
    sqlite_path = _project_sqlite_path(project, Path(content_root))
    return content_root, str(sqlite_path)


@router.get("/catalog", response_model=EngineeringCatalogResponse)
def engineering_catalog(
    project: str,
    request: Request,
    kind: Literal["quest", "dialogue_tree", "style_rubric"] | None = Query(default=None),
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1_000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> EngineeringCatalogResponse:
    content_root, sqlite_path = _project_paths(project)
    result = engineering_catalog_action(content_root, sqlite_path=sqlite_path)
    # Preserve the original complete catalog for non-browser clients. The workbench requests one
    # bounded collection at a time, keeping large worlds out of one payload and three unbounded
    # <select> elements.
    if kind is None and limit is None and cursor is None:
        return EngineeringCatalogResponse(project=project, **result)
    if kind is None:
        raise HTTPException(status_code=422, detail="工程目录分页需要 kind。")
    key = {
        "quest": "quest_ids",
        "dialogue_tree": "dialogue_tree_ids",
        "style_rubric": "style_rubrics",
    }[kind]
    try:
        page = paginate(result[key], limit=limit or 40, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    payload = {
        **result,
        "quest_ids": [],
        "dialogue_tree_ids": [],
        "style_rubrics": [],
        key: page.items,
    }
    return EngineeringCatalogResponse(
        project=project,
        kind=kind,
        total=page.total,
        next_cursor=page.next_cursor,
        snapshot_etag=page.snapshot_etag,
        **payload,
    )


@router.post("/quests/{quest_id}:explore", response_model=QuestExploreResponse)
def explore_project_quest(
    project: str,
    quest_id: str,
    req: QuestExploreRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> QuestExploreResponse:
    content_root, sqlite_path = _project_paths(project)
    try:
        result = quest_explore_action(
            content_root,
            quest_id=quest_id,
            max_states=req.max_states,
            max_depth=req.max_depth,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return QuestExploreResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.post(
    "/quests/{quest_id}/specs:translate",
    response_model=TemporalSpecTranslateResponse,
)
def translate_project_temporal_spec(
    project: str,
    quest_id: str,
    req: TemporalSpecTranslateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TemporalSpecTranslateResponse:
    if req.use_llm and req.llm_mode == "real":
        _require_real_allowed(request)
    content_root, sqlite_path = _project_paths(project)
    try:
        result = translate_temporal_spec_action(
            content_root,
            quest_id=quest_id,
            intent=req.intent,
            use_llm=req.use_llm,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model,
            max_cost_usd=req.max_cost_usd,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return TemporalSpecTranslateResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.post(
    "/quests/{quest_id}/specs:submit",
    response_model=TemporalSpecSubmitResponse,
)
def submit_project_temporal_spec(
    project: str,
    quest_id: str,
    req: TemporalSpecSubmitRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> TemporalSpecSubmitResponse:
    content_root, sqlite_path = _project_paths(project)
    try:
        result = submit_temporal_spec_action(
            content_root,
            quest_id=quest_id,
            intent=req.intent,
            draft=req.draft,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return TemporalSpecSubmitResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.get("/flags:xref", response_model=FlagXrefResponse)
def inspect_project_flags(
    project: str,
    request: Request,
    flag: str | None = Query(default=None, min_length=1, max_length=200),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> FlagXrefResponse:
    content_root, sqlite_path = _project_paths(project)
    try:
        result = flag_xref_action(
            content_root,
            flag=flag,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return FlagXrefResponse(project=project, **result)


@router.post("/dialogues/{tree_id}:walk", response_model=DialogueWalkResponse)
def walk_project_dialogue(
    project: str,
    tree_id: str,
    req: DialogueWalkRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> DialogueWalkResponse:
    content_root, sqlite_path = _project_paths(project)
    try:
        result = dialogue_walk_action(
            content_root,
            tree_id=tree_id,
            explore=req.mode == "explore",
            choices=req.choices,
            initial_state=req.initial_state,
            max_endings=req.max_endings,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return DialogueWalkResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.post("/repairs:plan", response_model=RepairPlanResponse)
def plan_project_repairs(
    project: str,
    req: RepairPlanRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> RepairPlanResponse:
    if req.llm_candidates and req.llm_mode == "real":
        _require_real_allowed(request)
    content_root, sqlite_path = _project_paths(project)
    result = repair_plan_action(
        content_root,
        max_iterations=req.max_iterations,
        max_depth=req.max_depth,
        seed=req.seed,
        llm_candidates=req.llm_candidates,
        max_llm_calls=req.max_llm_calls,
        llm_mode=req.llm_mode,
        llm_model=req.llm_model,
        max_cost_usd=req.max_cost_usd,
        sqlite_path=sqlite_path,
    )
    return RepairPlanResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.post("/quality:run", response_model=QualityHarnessResponse)
def run_project_quality_harness(
    project: str,
    req: QualityHarnessRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> QualityHarnessResponse:
    content_root, sqlite_path = _project_paths(project)
    result = quality_harness_action(
        content_root,
        propose_fixes=req.propose_fixes,
        max_issues=req.max_issues,
        max_candidates_per_issue=req.max_candidates_per_issue,
        sqlite_path=sqlite_path,
    )
    return QualityHarnessResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.post("/styles/{rubric_id}:sweep", response_model=StyleSweepResponse)
def sweep_project_style(
    project: str,
    rubric_id: str,
    req: StyleSweepRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> StyleSweepResponse:
    if req.use_llm and req.llm_mode == "real":
        _require_real_allowed(request)
    content_root, sqlite_path = _project_paths(project)
    try:
        result = style_sweep_action(
            content_root,
            rubric_id=rubric_id,
            semantic_threshold=req.semantic_threshold,
            use_llm=req.use_llm,
            max_judge=req.max_judge,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model,
            max_cost_usd=req.max_cost_usd,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return StyleSweepResponse(
        request_id=str(uuid.uuid4()),
        project=project,
        **result,
    )


@router.get("/agent-runs", response_model=AgentRunsResponse)
def list_project_agent_runs(
    project: str,
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=1_000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AgentRunsResponse:
    content_root, sqlite_path = _project_paths(project)
    snapshot = list_agent_runs_action(
        content_root,
        limit=0,
        offset=0,
        sqlite_path=sqlite_path,
    )
    loaded: dict[str, Any] = {}

    def load(offset: int, page_limit: int) -> list[dict[str, Any]]:
        result = list_agent_runs_action(
            content_root,
            limit=page_limit,
            offset=offset,
            sqlite_path=sqlite_path,
        )
        if result["snapshot_etag"] != snapshot["snapshot_etag"]:
            raise PaginationCursorError(
                "列表在翻页期间发生变化，请从第一页重新加载。",
                stale=True,
            )
        loaded.update(result)
        return result["runs"]

    try:
        page = paginate_window(
            total=int(snapshot["total"]),
            snapshot_etag=str(snapshot["snapshot_etag"]),
            limit=limit,
            cursor=cursor,
            load=load,
        )
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return AgentRunsResponse(
        project=project,
        runs=[AgentRunSummary.model_validate(item) for item in page.items],
        total=page.total,
        next_cursor=page.next_cursor,
        previous_cursor=page.previous_cursor,
        page_cursor=page.page_cursor,
        snapshot_etag=page.snapshot_etag,
        cost_budget=loaded.get("cost_budget", snapshot["cost_budget"]),
    )


@router.get("/agent-runs/{run_id}", response_model=AgentRunDetailResponse)
def get_project_agent_run(
    project: str,
    run_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> AgentRunDetailResponse:
    content_root, sqlite_path = _project_paths(project)
    try:
        result = get_agent_run_action(
            content_root,
            run_id=run_id,
            sqlite_path=sqlite_path,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return AgentRunDetailResponse(project=project, **result)
