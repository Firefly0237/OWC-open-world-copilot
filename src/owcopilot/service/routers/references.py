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

from fastapi import APIRouter, Header, HTTPException, Request

from ...app.actions import (
    add_reference_action,
    list_references_action,
    run_ingest_action,
    search_references_action,
)
from ..dependencies import _project_root_or_404
from ..schemas import IngestRequest, ReferenceAddRequest, ReferenceSearchRequest

router = APIRouter()


@router.get("/projects/{project}/references")
def list_references(
    project: str,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    return list_references_action(_project_root_or_404(project))


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
    )


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
