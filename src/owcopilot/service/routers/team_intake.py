"""Bearer-only team REST boundary for N2.1 source intake and human governance."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Annotated, Any, Literal, Never

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ...contracts import OwnershipScope
from ...intake import IntakeValidationError, SegmentDecision
from ...teamdb.errors import (
    RevisionConflict,
    TeamAuthorizationError,
    TeamDatabaseError,
    TeamDatabaseUnavailable,
    TeamIntakeConflict,
    TeamIntegrityError,
    TeamNotFoundError,
    TeamSchemaMismatch,
)
from ..pagination import PaginationCursorError, paginate
from ..team_auth import TeamIdentity, require_team_identity

router = APIRouter(prefix="/team", tags=["team-intake"])

_DEFAULT_SEGMENT_PAGE_SIZE = 50
_MAX_SEGMENT_PAGE_SIZE = 100


@dataclass(frozen=True)
class _SegmentPageParams:
    limit: int
    cursor: str | None


def _segment_page_params(
    limit: Annotated[int, Query(ge=1, le=_MAX_SEGMENT_PAGE_SIZE)] = (
        _DEFAULT_SEGMENT_PAGE_SIZE
    ),
    cursor: Annotated[str | None, Query(max_length=2048)] = None,
) -> _SegmentPageParams:
    return _SegmentPageParams(limit=limit, cursor=cursor)


class _StrictRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class RightsDeclarationRequest(_StrictRequest):
    rights_provenance: Literal["OWNED", "LICENSED", "THIRD_PARTY", "UNKNOWN"]
    declaration_text: str = Field(min_length=1)
    allowed_uses: dict[
        Literal["ANALYSIS", "RETRIEVAL", "DRAFTING", "TRAINING", "PUBLICATION"],
        bool,
    ] = Field(default_factory=dict)


class SegmentDecisionRequest(_StrictRequest):
    segment_id: str
    decision: Literal["CONFIRM", "OVERRIDE", "REJECT"]
    content_role: Literal["CANON_CANDIDATE", "REFERENCE", "MIXED", "UNKNOWN"]
    rationale: str = Field(min_length=1)


class SubmitIntakeRequest(_StrictRequest):
    # JSON arrays arrive as ``list``.  Keeping this field strict while declaring a
    # tuple makes every standards-compliant request fail validation under Pydantic.
    decisions: list[SegmentDecisionRequest] = Field(min_length=1)


class LineageAnchorRequest(_StrictRequest):
    segment_id: str
    lineage_kind: Literal["SOURCE_EVIDENCE", "INSPIRATION_LINEAGE"]
    target_ref: str = Field(min_length=1)


def _service(request: Request) -> Any:
    return request.app.state.team_intake_service


def _scope(identity: TeamIdentity, container_id: str) -> OwnershipScope:
    try:
        return OwnershipScope(tenant_id=identity.tenant_id, container_id=container_id)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_CONTAINER_ID", "message": "container_id must be a UUID"},
        ) from exc


def _run_id(value: str) -> str:
    try:
        # Reuse the strict service boundary without exposing an independent identity syntax.
        SegmentDecision(
            segment_id=value,
            decision="REJECT",
            content_role="UNKNOWN",
            rationale="identifier validation",
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_RUN_ID", "message": "run_id must be a UUID"},
        ) from exc
    return value


def _content_length(request: Request, *, limit: int) -> None:
    raw = request.headers.get("content-length")
    if raw is None:
        return
    if not raw.isascii() or not raw.isdecimal():
        raise HTTPException(
            status_code=400,
            detail={"code": "INVALID_CONTENT_LENGTH", "message": "Content-Length is invalid"},
        )
    if int(raw) > limit:
        raise HTTPException(
            status_code=413,
            detail={"code": "UPLOAD_TOO_LARGE", "message": "upload exceeds the byte limit"},
        )


def _result(value: object) -> Any:
    encoded = jsonable_encoder(value)
    if not isinstance(encoded, Mapping):
        raise HTTPException(
            status_code=503,
            detail={"code": "INTAKE_INTEGRITY_ERROR", "message": "invalid intake response"},
        )
    return dict(encoded)


def _page_error(exc: PaginationCursorError) -> HTTPException:
    return HTTPException(
        status_code=409 if exc.stale else 422,
        detail={
            "code": "STALE_CURSOR" if exc.stale else "INVALID_CURSOR",
            "message": str(exc),
        },
    )


def _mapping_rows(value: object) -> list[dict[str, object]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping)]


def _paged_result(value: object, params: _SegmentPageParams) -> dict[str, object]:
    """Bound the heavy segment arrays and bind cursors to the complete run snapshot."""

    result = _result(value)
    segments = _mapping_rows(result.get("segments"))
    if not segments:
        # OCR hand-off and rejected uploads have no collection to page; preserve their compact,
        # stable response contract instead of manufacturing unrelated empty arrays.
        return result
    correlated_keys = (
        "raw_triage_outputs",
        "triage_verdicts",
        "human_decisions",
        "lineage",
    )
    correlated = {key: _mapping_rows(result.get(key)) for key in correlated_keys}
    snapshot_marker = {
        "run_id": result.get("run_id"),
        "status": result.get("status"),
        "processing_code": result.get("processing_code"),
        "artifact": result.get("artifact"),
        "rights": result.get("rights"),
        "allowed_uses": result.get("allowed_uses"),
        "submitted": result.get("submitted"),
        "logical_deleted": result.get("logical_deleted"),
        "correlated": correlated,
    }
    try:
        page = paginate(
            segments,
            limit=params.limit,
            cursor=params.cursor,
            snapshot_items=[snapshot_marker, *segments],
        )
    except PaginationCursorError as exc:
        raise _page_error(exc) from exc
    visible_ids = {str(item.get("segment_id", "")) for item in page.items}
    result["segments"] = page.items
    for key, rows in correlated.items():
        if key in result:
            result[key] = [
                item for item in rows if str(item.get("segment_id", "")) in visible_ids
            ]
    result["segment_pagination"] = {
        "total": page.total,
        "limit": params.limit,
        "offset": page.offset,
        "page_cursor": page.page_cursor,
        "previous_cursor": page.previous_cursor,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }
    return result


def _raise_mapped(exc: Exception) -> Never:
    if isinstance(exc, IntakeValidationError):
        raise HTTPException(
            status_code=exc.status_code,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    if isinstance(exc, TeamAuthorizationError):
        raise HTTPException(
            status_code=403,
            detail={"code": "MEMBERSHIP_REQUIRED", "message": "editor membership required"},
        ) from exc
    if isinstance(exc, TeamNotFoundError):
        raise HTTPException(
            status_code=404,
            detail={"code": "INTAKE_NOT_FOUND", "message": "intake run not found"},
        ) from exc
    if isinstance(exc, RevisionConflict | TeamIntakeConflict):
        raise HTTPException(
            status_code=409,
            detail={"code": "INTAKE_CONFLICT", "message": "intake state is stale"},
        ) from exc
    if isinstance(exc, TeamDatabaseUnavailable):
        raise HTTPException(
            status_code=503,
            detail={"code": "DATABASE_UNAVAILABLE", "message": str(exc)},
        ) from exc
    if isinstance(exc, TeamSchemaMismatch):
        raise HTTPException(
            status_code=503,
            detail={"code": "SCHEMA_NOT_READY", "message": str(exc)},
        ) from exc
    if isinstance(exc, TeamIntegrityError):
        raise HTTPException(
            status_code=503,
            detail={"code": "INTAKE_INTEGRITY_ERROR", "message": str(exc)},
        ) from exc
    if isinstance(exc, TeamDatabaseError):
        raise HTTPException(
            status_code=503,
            detail={"code": "TEAM_DATABASE_ERROR", "message": str(exc)},
        ) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_INTAKE_REQUEST", "message": str(exc)},
        ) from exc
    raise exc


@router.post("/containers/{container_id}/intake-runs", status_code=201)
async def create_intake_run(
    container_id: Annotated[str, Path()],
    filename: Annotated[str, Query(min_length=1)],
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
    artifact_id: Annotated[str | None, Query()] = None,
) -> Any:
    intake_service = _service(request)
    _content_length(request, limit=intake_service.intake_policy.max_upload_bytes)
    try:
        values = {
            "actor_subject": identity.subject_id,
            "original_filename": filename,
            "mime_type": request.headers.get("content-type", ""),
            "chunks": request.stream(),
        }
        if artifact_id is None:
            value = await intake_service.create_intake_run_async(
                _scope(identity, container_id),
                **values,
            )
        else:
            value = await intake_service.create_replacement_intake_run_async(
                _scope(identity, container_id),
                artifact_id=artifact_id,
                **values,
            )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001 - translate only the closed domain taxonomy
        _raise_mapped(exc)


@router.get("/containers/{container_id}/intake-runs/{run_id}")
def read_intake_run(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
) -> Any:
    try:
        value = _service(request).read_intake_run(
            _scope(identity, container_id),
            subject_id=identity.subject_id,
            run_id=_run_id(run_id),
        )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post(
    "/containers/{container_id}/intake-runs/{run_id}/rights-declarations",
    status_code=201,
)
def declare_rights(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    body: RightsDeclarationRequest,
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
) -> Any:
    try:
        value = _service(request).declare_rights(
            _scope(identity, container_id),
            actor_subject=identity.subject_id,
            run_id=_run_id(run_id),
            rights_provenance=body.rights_provenance,
            declaration_text=body.declaration_text,
            allowed_uses=body.allowed_uses,
        )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/containers/{container_id}/intake-runs/{run_id}:submit")
def submit_intake(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    body: SubmitIntakeRequest,
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
) -> Any:
    try:
        decisions = tuple(
            SegmentDecision(
                segment_id=item.segment_id,
                decision=item.decision,
                content_role=item.content_role,
                rationale=item.rationale,
            )
            for item in body.decisions
        )
        value = _service(request).submit_human_decisions(
            _scope(identity, container_id),
            actor_subject=identity.subject_id,
            run_id=_run_id(run_id),
            decisions=decisions,
        )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/containers/{container_id}/intake-runs/{run_id}/lineage-anchors", status_code=201)
def create_lineage_anchor(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    body: LineageAnchorRequest,
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
) -> Any:
    try:
        value = _service(request).create_lineage_anchor(
            _scope(identity, container_id),
            actor_subject=identity.subject_id,
            run_id=_run_id(run_id),
            segment_id=body.segment_id,
            lineage_kind=body.lineage_kind,
            target_ref=body.target_ref,
        )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/containers/{container_id}/intake-runs/{run_id}:logical-delete")
def logical_delete_source_artifact(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
    page: Annotated[_SegmentPageParams, Depends(_segment_page_params)],
) -> Any:
    try:
        value = _service(request).logical_delete_source_artifact(
            _scope(identity, container_id),
            actor_subject=identity.subject_id,
            run_id=_run_id(run_id),
        )
        return _paged_result(value, page)
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


@router.post("/containers/{container_id}/intake-runs/{run_id}:authorize-drafting")
def authorize_drafting(
    container_id: Annotated[str, Path()],
    run_id: Annotated[str, Path()],
    request: Request,
    identity: Annotated[TeamIdentity, Depends(require_team_identity)],
) -> dict[str, object]:
    try:
        checked_run = _run_id(run_id)
        allowed = _service(request).is_use_allowed(
            _scope(identity, container_id),
            subject_id=identity.subject_id,
            run_id=checked_run,
            use_kind="DRAFTING",
        )
        if not allowed:
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "DRAFTING_NOT_AUTHORIZED",
                    "message": "source material is not authorized for drafting",
                },
            )
        return {"run_id": checked_run, "use_kind": "DRAFTING", "allowed": True}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        _raise_mapped(exc)


__all__ = ["router"]
