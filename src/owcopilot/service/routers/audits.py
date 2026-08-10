# 审计与问题清单：确定性审计入口 + 已落库问题查询。
# Deterministic audit + persisted issues.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...audit.context import AuditContext
from ...audit.default_rules import build_default_rule_registry
from ...audit.runner import AuditRunner
from ...content.hash import content_hash
from ...pipeline.audit import run_full_audit
from ..dependencies import _deterministic_cost_budget, _filter_issue_dicts, _open_project_context
from ..pagination import PaginationCursorError, paginate
from ..profile import require_local_capability
from ..schemas import ProjectAuditRequest, ProjectAuditResponse, ProjectIssuesResponse

router = APIRouter()


@router.post("/projects/{project}/audits", response_model=ProjectAuditResponse)
def create_project_audit(
    project: str,
    req: ProjectAuditRequest,
    request: Request,
    include_details: bool = Query(default=True),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectAuditResponse:
    if req.content is not None and req.persist:
        require_local_capability(request, "inline_issue_persistence")
    request_id = str(uuid.uuid4())

    if req.content is None:
        project_context = _open_project_context(project)
        if project_context is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"project {project!r} is not registered; configure "
                    "OWCOPILOT_PROJECTS_JSON or send inline content"
                ),
            )
        try:
            result = run_full_audit(project_context, persist=req.persist)
            return ProjectAuditResponse(
                request_id=request_id,
                project=project,
                content_hash=content_hash(project_context.bundle),
                audit_run=result.run.model_dump(mode="json"),
                issues=(
                    [issue.model_dump(mode="json") for issue in result.issues]
                    if include_details
                    else []
                ),
                totals=result.run.totals,
                cost_budget=_deterministic_cost_budget("audit_project"),
            )
        finally:
            project_context.close()

    runner = AuditRunner(build_default_rule_registry())
    result = runner.run(AuditContext.from_bundle(req.content))
    issues = [issue.model_dump(mode="json") for issue in result.issues]
    if req.persist:
        request.app.state.v2_issues[project] = issues
    return ProjectAuditResponse(
        request_id=request_id,
        project=project,
        content_hash=content_hash(req.content),
        audit_run=result.run.model_dump(mode="json"),
        issues=issues if include_details else [],
        totals=result.run.totals,
        cost_budget=_deterministic_cost_budget("audit_project"),
    )


@router.get("/projects/{project}/issues", response_model=ProjectIssuesResponse)
def list_project_issues(
    project: str,
    request: Request,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectIssuesResponse:
    project_context = _open_project_context(project)
    if project_context is not None:
        try:
            issues = [
                issue.model_dump(mode="json")
                for issue in project_context.sqlite_store.list_issues(
                    severity=severity,
                    rule_code=rule_code,
                    status=status,
                )
            ]
            try:
                page = paginate(issues, limit=limit, cursor=cursor)
            except PaginationCursorError as exc:
                raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
            return ProjectIssuesResponse(
                project=project,
                issues=page.items,
                total=page.total,
                next_cursor=page.next_cursor,
                snapshot_etag=page.snapshot_etag,
                cost_budget=_deterministic_cost_budget("list_issues"),
            )
        finally:
            project_context.close()

    require_local_capability(request, "unregistered_issue_storage")
    issues = _filter_issue_dicts(
        request.app.state.v2_issues.get(project, []),
        severity=severity,
        rule_code=rule_code,
        status=status,
    )
    try:
        page = paginate(issues, limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return ProjectIssuesResponse(
        project=project,
        issues=page.items,
        total=page.total,
        next_cursor=page.next_cursor,
        snapshot_etag=page.snapshot_etag,
        cost_budget=_deterministic_cost_budget("list_issues"),
    )
