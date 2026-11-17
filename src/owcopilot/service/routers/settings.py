# 服务商连接设置：查看/写入/探活（显式 local profile 语义）。
# Provider connection settings (explicit-local-profile scoped).

"""Domain router split out of ``service/api.py`` (FastAPI Bigger Applications).

Route paths, methods, status codes and response models are unchanged; this module only
relocates the handlers behind an ``APIRouter`` assembled by the app factory."""

from __future__ import annotations

import os
from typing import Any

from fastapi import APIRouter, Header, Request

from ...app.actions import probe_llm_connection_action
from ...llm.chat_model import resolve_chat_provider_kind
from ...llm.telemetry import prices_are_configured

# The dotenv reader stays in ``service.api`` so existing test monkeypatch seams keep
# resolving through that module namespace at call time.
from .. import api
from ..profile import require_local_capability
from ..schemas import ConnectionProbeRequest, ConnectionStatusResponse, ConnectionUpdateRequest

router = APIRouter()


def _connection_env(name: str) -> str:
    """Read the same project-prefixed-first contract as ``ChatModelProvider``."""
    return os.getenv(f"OWCOPILOT_{name}") or os.getenv(name) or ""


def _set_connection_env(name: str, value: str, *, clear_empty: bool) -> None:
    """Keep settings writes and the provider's precedence rules in lockstep."""
    normalized = value.strip()
    for key in (f"OWCOPILOT_{name}", name):
        if normalized:
            os.environ[key] = normalized
        elif clear_empty:
            os.environ.pop(key, None)


@router.get("/settings/connection", response_model=ConnectionStatusResponse)
def connection_status(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ConnectionStatusResponse:
    # status must mirror what a real call would see: the gateway dotenv-loads on
    # every real run, so do the same here or a repo .env reads as "not connected"
    api.load_dotenv()
    base_url = _connection_env("OPENAI_BASE_URL")
    return ConnectionStatusResponse(
        configured=bool(_connection_env("OPENAI_API_KEY").strip()),
        base_url=base_url,
        provider=resolve_chat_provider_kind(
            os.getenv("OWCOPILOT_LLM_PROVIDER"),
            base_url,
        ),
        prices_configured=prices_are_configured(),
    )


@router.post("/settings/connection", response_model=ConnectionStatusResponse)
def update_connection(
    req: ConnectionUpdateRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> ConnectionStatusResponse:
    """Wire the provider connection into an explicitly local-profile process.

    The profile is the trust boundary.  Request.client is commonly a reverse proxy,
    so loopback inference must not stand in for deployment authorization.
    """
    require_local_capability(request, "connection_settings_mutation")
    _set_connection_env("OPENAI_BASE_URL", req.base_url, clear_empty=True)
    _set_connection_env("OPENAI_API_KEY", req.api_key, clear_empty=False)
    provider = resolve_chat_provider_kind(req.provider, req.base_url.strip())
    os.environ["OWCOPILOT_LLM_PROVIDER"] = provider
    return ConnectionStatusResponse(
        configured=bool(_connection_env("OPENAI_API_KEY").strip()),
        base_url=_connection_env("OPENAI_BASE_URL"),
        provider=provider,
    )


@router.post("/settings/connection:probe")
def probe_connection(
    req: ConnectionProbeRequest,
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict[str, Any]:
    require_local_capability(request, "connection_settings_probe")
    return probe_llm_connection_action(
        base_url=req.base_url or _connection_env("OPENAI_BASE_URL"),
        api_key=req.api_key or _connection_env("OPENAI_API_KEY"),
        model=req.model,
        provider_kind=req.provider,
    )
