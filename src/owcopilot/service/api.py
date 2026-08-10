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
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

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
from .intake_tokens import IntakeTokenSigner
from .jobs import JobManager
from .profile import LOCAL_PROFILE, TEAM_PROFILE, deployment_profile, not_available_detail
from .schemas import (
    _LLMModeRequest,
)

__version__ = "0.2.0"

_DEFAULT_VITE_CORS_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"


def _configured_cors_origins(*, explicit_only: bool = False) -> list[str]:
    """Return the configured browser origins without granting implicit network access."""

    origins = list(
        dict.fromkeys(
            origin.strip()
            for origin in os.getenv("OWCOPILOT_CORS_ORIGINS", _DEFAULT_VITE_CORS_ORIGINS).split(",")
            if origin.strip()
        )
    )
    if explicit_only and "*" in origins:
        raise ValueError("team profile requires explicit OWCOPILOT_CORS_ORIGINS")
    return origins


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


# --------------------------------------------------------------------------- team composition
def _create_team_app(
    *,
    team_database: Any | None,
    team_repository: Any | None,
    team_intake_service: Any | None,
) -> FastAPI:
    """Build the PG-only team surface without constructing any local truth stores."""

    from ..intake import TeamIntakeService, build_team_triage_provider_from_env
    from ..teamdb import TeamDatabase, TeamIntakeRepository, TeamRepository

    database = team_database or TeamDatabase.from_env()
    repository = team_repository or TeamRepository(database)
    if team_intake_service is None:
        intake_repository = TeamIntakeRepository(database)
        intake_service = TeamIntakeService(
            intake_repository,
            triage_provider=build_team_triage_provider_from_env(),
        )
    else:
        intake_repository = None
        intake_service = team_intake_service

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # Record the startup check, but keep the process alive in a degraded state so /health
        # explains the fault and team reads can fail closed with a stable 503.
        app.state.team_startup_health = database.health()
        yield
        close = getattr(database, "close", None)
        if callable(close):
            close()

    app = FastAPI(
        title="owcopilot team canonical",
        version=__version__,
        description="PostgreSQL-backed team canonical API.",
        lifespan=lifespan,
    )
    # CORS permits the separately launched Vite UI to send its Bearer header; it does
    # not authenticate the request.  Team mode deliberately rejects wildcard origins.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_configured_cors_origins(explicit_only=True),
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type"],
    )
    app.state.deployment_profile = TEAM_PROFILE
    app.state.team_database = database
    app.state.team_repository = repository
    app.state.team_intake_repository = intake_repository
    app.state.team_intake_service = intake_service
    app.state.team_intake_triage_provider = getattr(intake_service, "triage_provider", None)

    frontend_dist = _frontend_dist_dir()
    if frontend_dist is None:
        # Keep the API-only mode honest: there is no pretend UI route when the built
        # bundle is absent, and every unsupported path still fails closed below.
        @app.get("/", tags=["system"])
        def team_root() -> dict[str, Any]:
            return {
                "service": "owcopilot",
                "version": __version__,
                "profile": TEAM_PROFILE,
                "canonical": "/team/containers/{container_uuid}/canonical",
                "intake": "/team/containers/{container_uuid}/intake-runs",
                "health": "/health",
                "ui": "NOT_AVAILABLE",
            }

    @app.get("/health", tags=["system"])
    def team_health() -> JSONResponse:
        health = database.health()
        detail: dict[str, object]
        if hasattr(health, "as_dict"):
            candidate = health.as_dict()
            detail = (
                dict(candidate)
                if isinstance(candidate, Mapping)
                else {"ready": False, "detail": "invalid_health_result"}
            )
        elif isinstance(health, Mapping):
            detail = dict(health)
        else:
            detail = {"ready": False, "detail": "invalid_health_result"}
        ready = detail.get("ready") is True
        return JSONResponse(
            status_code=200 if ready else 503,
            content={
                "status": "ok" if ready else "degraded",
                "version": __version__,
                "profile": TEAM_PROFILE,
                "database": detail,
            },
        )

    from .routers import team_canonical, team_intake

    app.include_router(team_canonical.router)
    app.include_router(team_intake.router)

    def _block_unmigrated(legacy_path: str) -> None:
        raise HTTPException(
            status_code=503,
            detail=not_available_detail(f"team_unmigrated_route:/{legacy_path}"),
        )

    if frontend_dist is not None:
        assets_dir = frontend_dist / "assets"
        if assets_dir.is_dir():
            app.mount(
                "/assets",
                _SpaStaticFiles(directory=str(assets_dir), asset_mount=True),
                name="team-spa-assets",
            )

        def _team_index_response() -> FileResponse:
            response = FileResponse(frontend_dist / "index.html")
            return _SpaStaticFiles._decorate(response, html=True, immutable_asset=False)

        @app.get("/", include_in_schema=False)
        def team_ui_root() -> RedirectResponse:
            return RedirectResponse(url="/intake", status_code=307)

        @app.get("/intake", include_in_schema=False)
        def team_intake_ui() -> FileResponse:
            return _team_index_response()

        @app.get("/canonical", include_in_schema=False)
        def team_canonical_ui() -> FileResponse:
            return _team_index_response()

    # Registered last, after the exact API, docs, health, asset, and /intake routes.
    # A built SPA must not turn unmigrated team GET endpoints into misleading 200s.
    @app.api_route(
        "/{legacy_path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    def block_unmigrated_team_surface(legacy_path: str) -> None:
        _block_unmigrated(legacy_path)

    return app


# --------------------------------------------------------------------------- app factory
def create_app(
    *,
    team_database: Any | None = None,
    team_repository: Any | None = None,
    team_intake_service: Any | None = None,
) -> FastAPI:
    profile = deployment_profile()
    if profile == TEAM_PROFILE:
        return _create_team_app(
            team_database=team_database,
            team_repository=team_repository,
            team_intake_service=team_intake_service,
        )
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
    cors_origins = _configured_cors_origins()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    limiter = _build_rate_limiter(int(os.getenv("OWCOPILOT_RATE_LIMIT_PER_MIN", "60")))
    app.state.limiter = limiter  # read by the global _require_client dependency
    app.state.service_cache = service_cache  # app-lifetime L1/L2 cache, shared by routers
    app.state.deployment_profile = profile
    if profile == LOCAL_PROFILE:
        # M0 stopgap for the untrusted HTTP extraction run -> submit hand-off. A fresh process
        # secret makes tokens short-lived capabilities rather than persisted truth; restart /
        # multi-instance intake continuity remains explicitly unavailable until N2.1. Team does
        # not construct this process-local signer because both synchronous endpoints are disabled.
        app.state.intake_tokens = IntakeTokenSigner(os.urandom(32))
        app.state.jobs_manager = JobManager()
        app.state.v2_issues = {}
    # WS-P control plane: SQLite (file path or :memory: via OWCOPILOT_PLATFORM_DB). Holds
    # only tenancy metadata + audit; the canon stays file-backed.
    platform_store = PlatformStore(os.getenv("OWCOPILOT_PLATFORM_DB", ":memory:"))
    app.state.platform_store = platform_store

    frontend_dist = _frontend_dist_dir()
    if frontend_dist is None:
        # No built frontend around: a browser hitting the API port should still learn
        # where to go instead of seeing a bare 404.
        @app.get("/", tags=["system"])
        def root() -> dict[str, Any]:
            return {
                "service": "owcopilot",
                "version": __version__,
                "hint": (
                    "这里是 OWCopilot API 服务。未发现已构建的前端"
                    "（npm --prefix apps/web run build 后重启即可单端口使用）；"
                    "开发模式请另起 npm --prefix apps/web run dev → http://localhost:5173。"
                    "交互式接口文档在 /docs，健康检查在 /health。"
                ),
                "docs": "/docs",
                "health": "/health",
            }

    @app.get("/health", tags=["system"])
    def health() -> dict[str, Any]:
        return {
            "status": "ok",
            "version": __version__,
            "llm_mode": llm_mode,
            "profile": profile,
            # Offline models are deterministic test doubles, never a hidden product fallback.
            "offline_llm_allowed": offline_llm_allowed(),
        }

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
        engineering,
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

    app.include_router(analytics.router, tags=["analytics"])
    app.include_router(assets.router, tags=["assets"])
    app.include_router(assist.router, tags=["assist"])
    app.include_router(audits.router, tags=["audits"])
    app.include_router(collab.router, tags=["collaboration"])
    app.include_router(compliance.router, tags=["compliance"])
    app.include_router(content_editing.router, tags=["content"])
    app.include_router(engineering.router)
    app.include_router(engine_import.router, tags=["imports"])
    app.include_router(exports.router, tags=["exports"])
    app.include_router(jobs.router, tags=["jobs"])
    app.include_router(localization.router, tags=["localization"])
    app.include_router(platform.router, tags=["platform"])
    app.include_router(references.router, tags=["references"])
    app.include_router(remediation.router, tags=["remediation"])
    app.include_router(retrieval.router, tags=["retrieval"])
    app.include_router(review.router, tags=["review"])
    app.include_router(settings.router, tags=["settings"])
    app.include_router(templates.router, tags=["templates"])
    app.include_router(views.router, tags=["views"])
    app.include_router(workspaces.router, tags=["workspaces"])

    if frontend_dist is not None:
        # One process, one port: the built Vue app ships from the API itself, so launch
        # is a single command with no CORS and no env vars. Mounted last — API routes
        # registered above keep precedence.
        app.mount("/", _SpaStaticFiles(directory=str(frontend_dist), html=True), name="spa")

    return app


app = create_app()
