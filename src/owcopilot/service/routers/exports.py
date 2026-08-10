# 导出：引擎文件导出 + 设定集下载。
# Engine-file exports + lorebook download.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import urllib.parse
import uuid
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException, Request, Response

from ...app.actions import (
    run_lorebook_export_action,
    run_project_export_action,
    run_prose_check_action,
)
from ...exporters import EngineTarget
from ...trust.security import PathSecurityError, resolve_under_root
from ..dependencies import _project_root_or_404
from ..schemas import (
    ProjectExportRequest,
    ProjectExportResponse,
    ProseCheckRequest,
    ProseCheckResponse,
)

router = APIRouter()


def _project_export_root(content_root: str | Path) -> Path:
    """Resolve the service-owned export root under the real project root.

    Starting the containment check at ``content_root`` (rather than at ``exports`` itself) is what
    rejects an existing ``.owcopilot``/``exports`` symlink before any exporter opens a file.
    """
    resolved_content_root = Path(content_root).expanduser().resolve()
    return resolve_under_root(
        resolved_content_root, resolved_content_root / ".owcopilot" / "exports"
    )


@router.post("/projects/{project}/exports", response_model=ProjectExportResponse)
def export_project_content(
    project: str,
    req: ProjectExportRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectExportResponse:
    request_id = str(uuid.uuid4())
    content_root = _project_root_or_404(project)
    try:
        engine = EngineTarget(req.target_engine)
    except ValueError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"unknown target engine {req.target_engine!r}; expected one of: "
                + ", ".join(item.value for item in EngineTarget)
            ),
        ) from e
    # Validate both the service-owned root and the engine child before the shared action writes.
    try:
        export_root = _project_export_root(content_root)
        resolve_under_root(export_root, export_root / engine.value)
    except PathSecurityError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    try:
        result = run_project_export_action(
            content_root,
            output_dir=export_root,
            target_engine=engine,
            unity_localization=req.unity_localization,
            row_struct_map=req.row_struct_map,
            planning_workbook=req.planning_workbook,
            planning_notes=req.planning_notes,
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    return ProjectExportResponse(request_id=request_id, project=project, **result)


@router.get("/projects/{project}/lorebook")
def download_lorebook(
    project: str,
    request: Request,
    fmt: str = "md",
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    if fmt not in ("md", "docx"):
        raise HTTPException(status_code=422, detail="fmt 仅支持 md 或 docx")
    content_root = _project_root_or_404(project)
    try:
        # Rendered fresh on every download so the file always matches the archive;
        # the copy under .owcopilot/exports doubles as a local audit trail.
        try:
            export_root = _project_export_root(content_root)
            out_dir = resolve_under_root(export_root, export_root / "lorebook")
        except PathSecurityError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        run_lorebook_export_action(content_root, output_dir=out_dir, formats=(fmt,))
        payload = (out_dir / f"lorebook.{fmt}").read_bytes()
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    media = (
        "text/markdown; charset=utf-8"
        if fmt == "md"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    encoded = urllib.parse.quote(f"{project}-设定集.{fmt}")
    return Response(
        content=payload,
        media_type=media,
        headers={
            "Content-Disposition": (
                f"attachment; filename=\"lorebook.{fmt}\"; filename*=UTF-8''{encoded}"
            )
        },
    )


@router.post("/projects/{project}/prose:check", response_model=ProseCheckResponse)
def check_project_prose(
    project: str,
    req: ProseCheckRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProseCheckResponse:
    """Check a pasted chapter against the archive: resolved mentions, forbidden-term spellings,
    unknown proper-noun candidates. Deterministic ($0, no LLM) — the action this route was
    missing (``run_prose_check_action`` existed in ``app/actions/export.py`` with no caller)."""
    request_id = str(uuid.uuid4())
    try:
        result = run_prose_check_action(_project_root_or_404(project), text=req.text)
    except ValueError as e:  # whitespace-only text passes min_length but check_prose refuses
        raise HTTPException(status_code=422, detail=str(e)) from e
    return ProseCheckResponse(request_id=request_id, project=project, **result)
