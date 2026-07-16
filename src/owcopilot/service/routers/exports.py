# 导出：引擎文件导出 + 设定集下载。
# Engine-file exports + lorebook download.

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import urllib.parse
import uuid

from fastapi import APIRouter, Header, HTTPException, Request, Response

from ...exporters import EngineTarget, export_content_bundle
from ...exporters.lorebook import write_lorebook
from ...pipeline.export_gate import assert_export_ready
from ...trust.security import PathSecurityError, resolve_under_root
from ..dependencies import _deterministic_cost_budget, _registered_project
from ..schemas import ProjectExportRequest, ProjectExportResponse

router = APIRouter()


@router.post("/projects/{project}/exports", response_model=ProjectExportResponse)
def export_project_content(
    project: str,
    req: ProjectExportRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ProjectExportResponse:
    request_id = str(uuid.uuid4())
    project_context = _registered_project(project)
    try:
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
        # Exports from the service land only inside the project's own runtime dir; the
        # caller never controls the output path (path-traversal hard stop).
        export_root = project_context.content_root / ".owcopilot" / "exports"
        try:
            output_dir = resolve_under_root(export_root, engine.value)
        except PathSecurityError as e:  # pragma: no cover - engine.value is enum-safe
            raise HTTPException(status_code=400, detail=str(e)) from e
        try:
            assert_export_ready(project_context)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        manifest = export_content_bundle(
            project_context.bundle,
            output_dir,
            target_engine=engine,
            unity_localization=req.unity_localization,
        )
        return ProjectExportResponse(
            request_id=request_id,
            project=project,
            output_dir=str(output_dir),
            manifest=manifest.model_dump(mode="json"),
            cost_budget=_deterministic_cost_budget("export_project"),
        )
    finally:
        project_context.close()


@router.get("/projects/{project}/lorebook")
def download_lorebook(
    project: str,
    request: Request,
    fmt: str = "md",
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> Response:
    if fmt not in ("md", "docx"):
        raise HTTPException(status_code=422, detail="fmt 仅支持 md 或 docx")
    project_context = _registered_project(project)
    try:
        # Rendered fresh on every download so the file always matches the archive;
        # the copy under .owcopilot/exports doubles as a local audit trail.
        out_dir = project_context.content_root / ".owcopilot" / "exports" / "lorebook"
        write_lorebook(project_context.bundle, out_dir, formats=(fmt,))
        payload = (out_dir / f"lorebook.{fmt}").read_bytes()
    finally:
        project_context.close()
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
