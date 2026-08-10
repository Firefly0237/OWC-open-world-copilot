# 人审队列（HITL）：列表/单项上下文/校准/裁决/反馈改写。
# Review queue: list/context/calibration/decide/revise.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import (
    decide_review_action,
    get_review_item_context_action,
    list_lessons_action,
    list_review_items_action,
    reviewer_calibration_action,
    revise_draft_action,
    revoke_lesson_action,
)
from ..api import _require_real_allowed
from ..dependencies import _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import (
    CalibrationContextResp,
    LessonRevokeRequest,
    PayloadStageSummary,
    PayloadSummary,
    ReviewDecideRequest,
    ReviewDecideResponse,
    ReviewItemContextResponse,
    ReviewItemsResponse,
    ReviewReviseRequest,
    ReviewReviseResponse,
)

router = APIRouter()


@router.get("/projects/{project}/review_items", response_model=ReviewItemsResponse)
def list_review_items(
    project: str,
    request: Request,
    limit: int = Query(default=50, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ReviewItemsResponse:
    result = list_review_items_action(_project_root_or_404(project))
    try:
        page = paginate(result["items"], limit=limit, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return ReviewItemsResponse(
        project=project,
        count=page.total,
        items=page.items,
        next_cursor=page.next_cursor,
        snapshot_etag=page.snapshot_etag,
        cost_budget=result["cost_budget"],
    )


@router.get(
    "/projects/{project}/review_items/{item_id}:context",
    response_model=ReviewItemContextResponse,
)
def get_review_item_context(
    project: str,
    item_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ReviewItemContextResponse:
    """Read-only aggregated context for a single review item (IN-4).

    Returns payload summary, issue refs, critic verdict/score, refine trail reflection
    and calibration statistics. Pure GET; no writes, idempotent, no side effects.
    """
    try:
        result = get_review_item_context_action(_project_root_or_404(project), item_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"review item not found: {e}") from e
    # Coerce nested dicts into the response model
    payload_summary_raw = result.get("payload_summary", {})
    stages = [PayloadStageSummary(**s) for s in payload_summary_raw.get("stages", [])]
    cal_raw = result.get("calibration_context", {})
    return ReviewItemContextResponse(
        item_id=result["item_id"],
        item_type=result["item_type"],
        status=result["status"],
        payload_summary=PayloadSummary(
            title=payload_summary_raw.get("title"),
            objective=payload_summary_raw.get("objective"),
            stages=stages,
            summary=payload_summary_raw.get("summary"),
        ),
        issue_refs=result.get("issue_refs", []),
        critic_verdict=result.get("critic_verdict"),
        critic_score=result.get("critic_score"),
        refine_trail_last_reflection=result.get("refine_trail_last_reflection"),
        calibration_context=CalibrationContextResp(
            item_type=cal_raw["item_type"],
            false_pass_rate=cal_raw.get("false_pass_rate"),
            sample_size=cal_raw["sample_size"],
            sufficient_sample=cal_raw["sufficient_sample"],
        ),
    )


@router.get("/projects/{project}/review/calibration")
def review_calibration(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = reviewer_calibration_action(_project_root_or_404(project))
    return {"project": project, **result}


@router.post("/projects/{project}/review/calibration:learn")
def learn_from_review_calibration(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Explicitly distil calibration false-passes into prompt lessons (a deliberate write)."""
    result = reviewer_calibration_action(_project_root_or_404(project), write_lessons=True)
    return {"project": project, **result}


@router.get("/projects/{project}/lessons")
def list_project_lessons(
    project: str,
    request: Request,
    include_revoked: bool = False,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = list_lessons_action(_project_root_or_404(project), include_revoked=include_revoked)
    # Keep the original unpaged response byte-shape for CLI/older clients.  Supplying either
    # pagination parameter opts into a bounded, snapshot-bound cursor response.
    if limit is None and cursor is None:
        return {"project": project, **result}
    try:
        page = paginate(result["lessons"], limit=limit or 50, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    result["count"] = page.total
    result["total"] = page.total
    result["lessons"] = page.items
    result["next_cursor"] = page.next_cursor
    result["snapshot_etag"] = page.snapshot_etag
    return {"project": project, **result}


@router.post("/projects/{project}/lessons/{lesson_id}:revoke")
def revoke_project_lesson(
    project: str,
    lesson_id: str,
    req: LessonRevokeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = revoke_lesson_action(
            _project_root_or_404(project), lesson_id=lesson_id, reason=req.reason
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"project": project, **result}


@router.post(
    "/projects/{project}/review_items/{item_id}:decide",
    response_model=ReviewDecideResponse,
)
def decide_review_item_endpoint(
    project: str,
    item_id: str,
    req: ReviewDecideRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ReviewDecideResponse:
    """The HITL write path over REST: decisions are final (the backend guard turns a
    double decide into 409, so client retries cannot corrupt provenance)."""
    try:
        decided = decide_review_action(
            _project_root_or_404(project),
            item_id=item_id,
            decision=req.decision,
            operator=req.operator,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"review item not found: {e}") from e
    except ValueError as e:
        raise _manage_error(e) from e
    return ReviewDecideResponse(
        project=project,
        item_id=item_id,
        decision=str(decided.get("decision", req.decision)),
        written_ref=decided.get("written_ref"),
        post_audit_open_errors=int(decided.get("post_audit_open_errors", 0)),
        cost_budget=decided.get("cost_budget") or {},
    )


@router.post(
    "/projects/{project}/review_items/{item_id}:revise",
    response_model=ReviewReviseResponse,
)
def revise_review_item_endpoint(
    project: str,
    item_id: str,
    req: ReviewReviseRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ReviewReviseResponse:
    """Feedback-driven revision: the reviewer asks for changes and the draft is regenerated in
    place, staying pending so a human still approves it (never an auto-land)."""
    if req.llm_mode == "real":
        _require_real_allowed(request)
    try:
        revised = revise_draft_action(
            _project_root_or_404(project),
            item_id=item_id,
            feedback=req.feedback,
            budget_tokens=req.budget_tokens,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model,
        )
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"review item not found: {e}") from e
    except ValueError as e:
        raise _manage_error(e) from e
    return ReviewReviseResponse(
        project=project,
        item_id=item_id,
        item=revised.get("item") or {},
        revised_payload=revised.get("revised_payload") or {},
        cost_budget=revised.get("cost_budget") or {},
    )
