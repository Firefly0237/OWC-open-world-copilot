# 灵感参考库：列出/新增/检索 + 严格表格导入。
# Inspiration references: list/add/search + table ingest.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Query, Request

from ...app.actions import (
    add_reference_action,
    approve_reference_action,
    list_references_action,
    remove_reference_action,
    run_ingest_action,
    search_references_action,
    upload_reference_action,
)
from ...intake import IntakeValidationError
from ..dependencies import _manage_error, _project_root_or_404
from ..pagination import PaginationCursorError, paginate
from ..schemas import (
    IngestRequest,
    ReferenceAddRequest,
    ReferenceApproveRequest,
    ReferenceSearchRequest,
    ReferenceUploadRequest,
    ReferenceUploadResponse,
)

router = APIRouter()


@router.get("/projects/{project}/references")
def list_references(
    project: str,
    request: Request,
    limit: int | None = Query(default=None, ge=1, le=200),
    cursor: str | None = Query(default=None, max_length=1000),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    result = list_references_action(_project_root_or_404(project))
    # Keep the original all-at-once response for CLI/older clients.  The browser always opts into
    # a bounded page with ``limit`` so a large reference library cannot create an unbounded DOM.
    if limit is None and cursor is None:
        return result
    try:
        page = paginate(result["sources"], limit=limit or 50, cursor=cursor)
    except PaginationCursorError as exc:
        raise HTTPException(status_code=409 if exc.stale else 422, detail=str(exc)) from exc
    return {
        **result,
        "count": page.total,
        "sources": page.items,
        "total": page.total,
        "next_cursor": page.next_cursor,
        "snapshot_etag": page.snapshot_etag,
    }


@router.post("/projects/{project}/references", status_code=201)
def add_reference(
    project: str,
    req: ReferenceAddRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return add_reference_action(
        _project_root_or_404(project),
        title=req.title,
        text=req.text,
        source_type=req.source_type,
        original_filename=req.original_filename,
        allowed_uses=req.allowed_uses,
        provenance=req.provenance,
    )


@router.post(
    "/projects/{project}/references:upload",
    status_code=201,
    response_model=ReferenceUploadResponse,
)
def upload_reference(
    project: str,
    req: ReferenceUploadRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Upload bytes while preserving formal-intake locators whenever the format supports them."""
    try:
        raw = base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"文件内容不是合法的 base64：{e}") from e
    safe_name = Path(req.filename).name
    if not safe_name or safe_name != req.filename:
        raise HTTPException(status_code=400, detail="文件名不能为空且不能包含目录。")
    try:
        return upload_reference_action(
            _project_root_or_404(project),
            title=req.title,
            content=raw,
            original_filename=safe_name,
            mime_type=req.mime_type,
            allowed_uses=req.allowed_uses,
            provenance=req.provenance,
        )
    except IntakeValidationError as exc:
        raise HTTPException(
            status_code=exc.status_code,
            detail={
                "code": exc.code,
                "message": f"文件解析失败（{safe_name}）：{exc}",
                "ingestion_mode": _failed_upload_mode(exc.code),
                "degraded": True,
                "degradation_reasons": [
                    _upload_degradation_reason(exc.code),
                    exc.code.casefold(),
                ],
            },
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "REFERENCE_EXTRACTION_FAILED",
                "message": f"文件解析失败（{safe_name}）：{exc}",
                "ingestion_mode": "legacy_text",
                "degraded": True,
                "degradation_reasons": ["legacy_reference_extraction_failed"],
            },
        ) from exc


def _upload_degradation_reason(code: str) -> str:
    if code == "PDF_OCR_REQUIRED":
        return "ocr_backend_not_available"
    if code.endswith("BACKEND_UNAVAILABLE"):
        return "structured_parser_backend_unavailable"
    if code in {"UNSUPPORTED_EXTENSION", "UNSUPPORTED_REFERENCE_FORMAT"}:
        return "unsupported_reference_format"
    return "structured_intake_rejected"


def _failed_upload_mode(code: str) -> str:
    if code in {
        "EMPTY_UPLOAD",
        "INVALID_FILENAME",
        "UNSUPPORTED_EXTENSION",
        "UNSUPPORTED_REFERENCE_FORMAT",
    }:
        return "rejected_unparsed"
    return "structured_locator"


@router.delete("/projects/{project}/references/{reference_id}")
def delete_reference(
    project: str,
    reference_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = remove_reference_action(_project_root_or_404(project), source_id=reference_id)
    except ValueError as e:
        raise _manage_error(e) from e
    return {"project": project, **result}


@router.post("/projects/{project}/references/{reference_id}:approve")
def approve_reference(
    project: str,
    reference_id: str,
    req: ReferenceApproveRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    try:
        result = approve_reference_action(
            _project_root_or_404(project),
            source_id=reference_id,
            allowed_uses=req.allowed_uses,
            note=req.note,
        )
    except ValueError as exc:
        raise _manage_error(exc) from exc
    return {"project": project, **result}


@router.post("/projects/{project}/references:search")
def search_references(
    project: str,
    req: ReferenceSearchRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return search_references_action(
        _project_root_or_404(project), query=req.query, budget_tokens=req.budget_tokens
    )


@router.post("/projects/{project}/ingest")
def ingest_table(
    project: str,
    req: IngestRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    """Strict-format table import. The upload lands in the project's own tmp dir and
    the parser reads it by extension; the caller never controls a path on the server."""
    content_root = _project_root_or_404(project)
    try:
        raw = base64.b64decode(req.content_base64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"invalid base64: {e}") from e
    # strip any directory parts the client sent; keep only the bare name + extension
    safe_name = Path(req.filename).name
    if not safe_name or safe_name.startswith("."):
        raise HTTPException(status_code=400, detail="filename must have a name and extension")
    tmp_dir = Path(content_root) / ".owcopilot" / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / safe_name
    tmp_path.write_bytes(raw)
    try:
        return run_ingest_action(
            content_root,
            paths=[str(tmp_path)],
            dry_run=req.dry_run,
            write_non_conflicting=req.write_non_conflicting,
        )
    except (ValueError, KeyError) as e:
        raise HTTPException(status_code=422, detail=f"import failed: {e}") from e
    finally:
        tmp_path.unlink(missing_ok=True)


# --- WS-P platform control plane (additive; existing endpoints keep the X-API-Key loopback) ---
