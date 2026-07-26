# FastAPI 服务：v2 内容工作台 API（前端与外部集成的主入口）。
# 约定：读/生成/审计走这里；写 canon 一律经审阅队列，不在 handler 里直接落库。
"""FastAPI service: the v2 content-workbench API.

The v2 surface is resource-oriented per registered project (`OWCOPILOT_PROJECTS_JSON` maps
project ids to content roots; request bodies never carry filesystem paths):

    POST /projects/{p}/audits                       deterministic audit (persist optional)
    GET  /projects/{p}/issues                       persisted issues with filters
    POST /projects/{p}/context:pack                 hybrid retrieval context pack
    POST /projects/{p}/ask                          cited lore QA (refuses when ungrounded)
    POST /projects/{p}/impact:analyze               change blast-radius (pure graph)
    POST /projects/{p}/issues/{id}/suggestions      shadow-validated fix candidates
    POST /projects/{p}/patches/{id}:apply|:rollback human write path, operator recorded
    POST /projects/{p}/contents/quests:draft        AI draft -> review queue (never direct write)
    POST /projects/{p}/assist/barks:batch           lint-filtered bark variants -> review queue
    POST /projects/{p}/exports                      engine files under the project runtime dir

Every model call goes through the single `LLMGateway` chokepoint backed by the app-lifetime
cache, so cost control and telemetry stay uniform. Offline by default ($0, no keys; deterministic
providers) for CI and the docker smoke test. Real mode is gated per request: `llm_mode=real`
refuses unless `OWCOPILOT_API_KEY` gates the service.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware

from ..config import DEFAULT_CHEAP_MODEL
from ..llm.gateway import (
    OFFLINE_LLM_FORBIDDEN_MESSAGE,
    LLMGateway,
    offline_llm_allowed,
)
from ..llm.resilience import build_real_provider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..platform import (
    PlatformStore,
)
from ..util import load_dotenv
from .dependencies import (
    _build_cache,
    _build_rate_limiter,
    _frontend_dist_dir,
    _llm_mode,
    _require_client,
    _SpaStaticFiles,
)
from .jobs import JobManager
from .schemas import (
    _LLMModeRequest,
)

__version__ = "0.2.0"


# --------------------------------------------------------------------------- access control
def _is_loopback(request: Request) -> bool:
    """True for the single-user-on-this-machine case. The real-mode fail-closed gate
    exists to stop strangers from spending a *deployed* server's provider credit; the
    person sitting at localhost owns both the machine and the key. ("testclient" is
    starlette's TestClient host — tests run in-process, same trust domain.)"""
    host = request.client.host if request.client else ""
    return host in {"127.0.0.1", "::1", "localhost", "testclient"}


def _require_real_allowed(request: Request) -> None:
    if not os.getenv("OWCOPILOT_API_KEY") and not _is_loopback(request):
        raise HTTPException(
            status_code=403,
            detail=(
                "llm_mode=real over the network requires OWCOPILOT_API_KEY to be "
                "configured (fail-closed: real mode spends provider credit)"
            ),
        )
    # the gateway dotenv-loads before every real call, so the gate must judge by the
    # same view or it would 503 a request that was about to succeed
    load_dotenv()
    if not os.getenv("OPENAI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="真实模型未配置：先在「设置」接入服务商与 API Key。",
        )


def _require_offline_allowed() -> None:
    """Fail closed before an endpoint would serve fake LLM output. The offline doubles are a
    test/CI fixture, not a product mode, so a real deployment (no opt-in) gets a clear 'connect a
    model' error instead of canned text presented as the model's answer."""
    if not offline_llm_allowed():
        raise HTTPException(status_code=503, detail=OFFLINE_LLM_FORBIDDEN_MESSAGE)


def _task_gateway(
    req: _LLMModeRequest,
    *,
    request: Request,
    task: str,
    offline_provider: Any,
    namespace: str = "",
    extra_tasks: list[str] | None = None,
) -> tuple[LLMGateway | None, TelemetryCollector]:
    """Per-request gateway for v2 assist tasks. `offline_provider=None` with offline mode
    means the caller runs deterministically without any gateway (suggest).

    Real mode uses the same loopback-aware gate as every other generator: localhost owns
    the key, a remote caller must set OWCOPILOT_API_KEY (fail-closed), and either way a
    provider must be configured. The shared app-lifetime cache backs every request, so
    repeated questions/briefs hit L1/L2 instead of the provider."""
    telemetry = TelemetryCollector()
    if req.llm_mode == "real":
        _require_real_allowed(request)
        model: str = req.llm_model or os.getenv("OWCOPILOT_CHEAP_MODEL") or DEFAULT_CHEAP_MODEL
        provider: Any = build_real_provider(model=model)
    elif offline_provider is None:
        return None, telemetry
    else:
        _require_offline_allowed()
        provider = offline_provider
    mapping = {task: "cheap"}
    for extra in extra_tasks or []:
        mapping[extra] = "cheap"
    gateway = LLMGateway(
        providers={"cheap": provider},
        router=StaticRouter(mapping=mapping),
        cache=request.app.state.service_cache,
        telemetry=telemetry,
        max_retries=1 if req.llm_mode == "real" else 0,
        retry_backoff_seconds=1.0 if req.llm_mode == "real" else 0.0,
        namespace=namespace,
    )
    return gateway, telemetry


# --------------------------------------------------------------------------- app factory
def create_app() -> FastAPI:
    llm_mode = _llm_mode()
    service_cache = _build_cache()

    app = FastAPI(
        title="owcopilot",
        version=__version__,
        description="Consistency-checked quest generation for open-world game development.",
        # One global gate for every API route (public paths self-skip). Replaces the auth +
        # rate-limit preamble that used to be copy-pasted into each endpoint.
        dependencies=[Depends(_require_client)],
    )
    # Browser clients (the Vue frontend) live on another origin in dev; CORS is opt-out
    # via env. The API key gate still applies — CORS only governs who may *ask*.
    cors_origins = [
        origin.strip()
        for origin in os.getenv(
            "OWCOPILOT_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if origin.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    limiter = _build_rate_limiter(int(os.getenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "60")))
    app.state.limiter = limiter  # read by the global _require_client dependency
    app.state.service_cache = service_cache  # app-lifetime L1/L2 cache, shared by routers
    jobs_manager = JobManager()
    app.state.jobs_manager = jobs_manager
    app.state.v2_issues = {}
    # WS-P control plane: SQLite (file path or :memory: via OWCOPILOT_PLATFORM_DB). Holds
    # only tenancy metadata + audit; the canon stays file-backed.
    platform_store = PlatformStore(os.getenv("OWCOPILOT_PLATFORM_DB", ":memory:"))
    app.state.platform_store = platform_store

    frontend_dist = _frontend_dist_dir()
    if frontend_dist is None:
        # No built frontend around: a browser hitting the API port should still learn
        # where to go instead of seeing a bare 404.
        @app.get("/")
        def root() -> dict[str, Any]:
            return {
                "service": "owcopilot",
                "version": __version__,
                "hint": (
                    "这里是 OWCopilot API 服务。未发现已构建的前端"
                    "（npm --prefix frontend run build 后重启即可单端口使用）；"
                    "开发模式请另起 npm --prefix frontend run dev → http://localhost:5173。"
                    "交互式接口文档在 /docs，健康检查在 /health。"
                ),
                "docs": "/docs",
                "health": "/health",
            }

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "llm_mode": llm_mode}

    # --- domain routers (composition root; imported in-factory to avoid a module
    # load cycle since router modules import the real-mode gate from this module) ---
    from .routers import (
        analytics,
        assets,
        assist,
        audits,
        collab,
        compliance,
        content_editing,
        engine_import,
        exports,
        jobs,
        localization,
        platform,
        references,
        remediation,
        retrieval,
        review,
        settings,
        templates,
        views,
        workspaces,
    )

    app.include_router(analytics.router)
    app.include_router(assets.router)
    app.include_router(assist.router)
    app.include_router(audits.router)
    app.include_router(collab.router)
    app.include_router(compliance.router)
    app.include_router(content_editing.router)
    app.include_router(engine_import.router)
    app.include_router(exports.router)
    app.include_router(jobs.router)
    app.include_router(localization.router)
    app.include_router(platform.router)
    app.include_router(references.router)
    app.include_router(remediation.router)
    app.include_router(retrieval.router)
    app.include_router(review.router)
    app.include_router(settings.router)
    app.include_router(templates.router)
    app.include_router(views.router)
    app.include_router(workspaces.router)

    if frontend_dist is not None:
        # One process, one port: the built Vue app ships from the API itself, so launch
        # is a single command with no CORS and no env vars. Mounted last — API routes
        # registered above keep precedence.
        app.mount("/", _SpaStaticFiles(directory=str(frontend_dist), html=True), name="spa")

    return app


app = create_app()
