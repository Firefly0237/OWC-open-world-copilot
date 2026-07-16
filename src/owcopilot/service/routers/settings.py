# 服务商连接设置：查看/写入/探活（本机语义）。
# Provider connection settings (loopback-scoped).

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from ...app.actions import probe_llm_connection_action
from ...llm.telemetry import prices_are_configured

# The real-mode / dotenv gate stays in ``service.api`` so the test monkeypatch seams
# (``owcopilot.service.api._is_loopback`` / ``.load_dotenv``) keep resolving through that module
# namespace at call time; reach them via the module object rather than a bound name.
from .. import api
from ..schemas import ConnectionProbeRequest, ConnectionStatusResponse, ConnectionUpdateRequest

router = APIRouter()


@router.get("/settings/connection", response_model=ConnectionStatusResponse)
def connection_status(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ConnectionStatusResponse:
    # status must mirror what a real call would see: the gateway dotenv-loads on
    # every real run, so do the same here or a repo .env reads as "not connected"
    api.load_dotenv()
    return ConnectionStatusResponse(
        configured=bool(os.getenv("OPENAI_API_KEY", "").strip()),
        base_url=os.getenv("OPENAI_BASE_URL", ""),
        prices_configured=prices_are_configured(),
    )


@router.post("/settings/connection", response_model=ConnectionStatusResponse)
def update_connection(
    req: ConnectionUpdateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ConnectionStatusResponse:
    """Wire the provider connection into the server process (same mechanism the
    legacy UI used). Local-machine semantics: only loopback may change it — a remote
    client must never reconfigure whose key this server spends."""
    if not api._is_loopback(request):
        raise HTTPException(status_code=403, detail="connection settings are local-only")
    if req.base_url.strip():
        os.environ["OPENAI_BASE_URL"] = req.base_url.strip()
    if req.api_key.strip():
        os.environ["OPENAI_API_KEY"] = req.api_key.strip()
    return ConnectionStatusResponse(
        configured=bool(os.getenv("OPENAI_API_KEY", "").strip()),
        base_url=os.getenv("OPENAI_BASE_URL", ""),
    )


@router.post("/settings/connection:probe")
def probe_connection(
    req: ConnectionProbeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    if not api._is_loopback(request) and not os.getenv("OWCOPILOT_API_KEY"):
        raise HTTPException(status_code=403, detail="probe is local-only on open servers")
    return probe_llm_connection_action(
        base_url=req.base_url or os.getenv("OPENAI_BASE_URL", ""),
        api_key=req.api_key or os.getenv("OPENAI_API_KEY", ""),
        model=req.model,
    )
