# 变更影响与补丁工作流：影响面分析 + 修复候选 + 应用/回滚。
# Impact + suggestions + patch apply/rollback.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import list_patches_action
from ...impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ...pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ...telemetry import deterministic_step, llm_step, summarize_workflow
from ..api import _task_gateway
from ..dependencies import _deterministic_cost_budget, _project_root_or_404, _registered_project
from ..pagination import PaginationCursorError, paginate
from ..schemas import (
    ProjectImpactRequest,
    ProjectImpactResponse,
    ProjectPatchApplyResponse,
    ProjectPatchDecisionRequest,
    ProjectPatchRollbackResponse,
    ProjectSuggestRequest,
    ProjectSuggestResponse,
)

router = APIRouter()


@router.get("/projects/{project}/patches")
def list_project_patches(
    project: str,
    request: Request,
    status: str | None = Query(default=None, max_length=40),
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, object]:
    """List persisted repair proposals so agent-created patches can always be recovered."""
    result = list_patches_action(_project_root_or_404(project), status=status)
    try:
        page = paginate(result["patches"], limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        "project": project,
        "patches": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
        "cost_budget": result["cost_budget"],
    }


@router.post("/projects/{project}/impact:analyze", response_model=ProjectImpactResponse)
def analyze_project_impact(
    project: str,
    req: ProjectImpactRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectImpactResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        changes: list[Change] = []
        for spec in req.changes:
            try:
                change_type = ChangeType(spec.change_type)
            except ValueError as e:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"unknown change type {spec.change_type!r}; expected one of: "
                        + ", ".join(item.value for item in ChangeType)
                    ),
                ) from e
            changes.append(Change(change_type=change_type, target_ref=spec.target_ref))
        result = ImpactAnalyzer(project_context.graph).analyze(
            ChangeSet(changes=changes), max_depth=req.max_depth
        )
        return ProjectImpactResponse(
            request_id=request_id,
            project=project,
            must_change=[
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.MUST_CHANGE)
            ],
            suggest_check=[
                item.model_dump(mode="json") for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
            ],
            total=len(result.items),
            cost_budget=_deterministic_cost_budget("impact_of"),
        )
    finally:
        project_context.close()


@router.post(
    "/projects/{project}/issues/{issue_id}/suggestions",
    response_model=ProjectSuggestResponse,
)
def create_issue_suggestions(
    project: str,
    issue_id: str,
    req: ProjectSuggestRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectSuggestResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        try:
            issue = find_issue(project_context, issue_id)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        gateway, telemetry = _task_gateway(
            req, request=request, task="patch_suggest", offline_provider=None, namespace=project
        )
        result = suggest_for_issue(
            project_context,
            issue,
            gateway=gateway,
            max_candidates=req.max_candidates,
            budget_tokens=req.budget_tokens,
        )
        telemetry_summary = telemetry.summary()
        cost_budget = (
            summarize_workflow(
                [llm_step("patch_suggest", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget
            if result.used_llm
            else summarize_workflow([deterministic_step("patch_suggest")]).budget
        )
        return ProjectSuggestResponse(
            request_id=request_id,
            project=project,
            issue_id=issue_id,
            candidates=[
                {
                    "patch_id": ranked.candidate.id,
                    "source": ranked.source,
                    "target_resolved": ranked.target_resolved,
                    "resolved_error_count": len(ranked.resolved_errors),
                    "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                    "rationale": ranked.candidate.rationale,
                }
                for ranked in result.candidates
            ],
            rejected_count=result.rejected_count,
            parse_failed=result.parse_failed,
            used_llm=result.used_llm,
            telemetry=telemetry_summary,
            cost_budget=cost_budget.model_dump(mode="json"),
        )
    finally:
        project_context.close()


@router.post(
    "/projects/{project}/patches/{patch_id}:apply",
    response_model=ProjectPatchApplyResponse,
)
def apply_project_patch(
    project: str,
    patch_id: str,
    req: ProjectPatchDecisionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectPatchApplyResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        try:
            outcome = apply_patch_workflow(project_context, patch_id, operator=req.operator)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return ProjectPatchApplyResponse(
            request_id=request_id,
            project=project,
            applied=outcome.applied,
            patch_id=outcome.patch_id,
            reason=outcome.reason,
            introduced_errors=outcome.introduced_errors,
            resolved_errors=outcome.resolved_errors,
            post_audit_open_errors=outcome.post_audit_open_errors,
            cost_budget=_deterministic_cost_budget("apply_patch"),
        )
    finally:
        project_context.close()


@router.post(
    "/projects/{project}/patches/{patch_id}:rollback",
    response_model=ProjectPatchRollbackResponse,
)
def rollback_project_patch(
    project: str,
    patch_id: str,
    req: ProjectPatchDecisionRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectPatchRollbackResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
        try:
            outcome = rollback_patch_workflow(project_context, patch_id, operator=req.operator)
        except FileNotFoundError as e:
            raise HTTPException(status_code=404, detail=str(e)) from e
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return ProjectPatchRollbackResponse(
            request_id=request_id,
            project=project,
            rolled_back=outcome.rolled_back,
            patch_id=outcome.patch_id,
            post_audit_open_errors=outcome.post_audit_open_errors,
            cost_budget=_deterministic_cost_budget("rollback_patch"),
        )
    finally:
        project_context.close()
