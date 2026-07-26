# 服务层共享依赖：鉴权/限流门、项目解析、成本预算等被各域 router 复用的横切件。
# Shared FastAPI dependencies and cross-cutting helpers used by every domain router.
"""Composition-root-independent building blocks for the v2 content-workbench API.

Following FastAPI's "Bigger Applications" layout, cross-cutting concerns live here rather than in
the app factory: the auth + rate-limit gate (:func:`_require_client`), project-id resolution
(:func:`_open_project_context`, :func:`_project_root_or_404`), the tenancy principal resolver and
role gate, and a handful of small pure helpers. Routers depend on these via ``Depends`` or plain
calls; the app factory wires the per-process resources onto ``app.state``.

The real-mode admission gate (``_require_real_allowed`` / ``_is_loopback`` / ``load_dotenv``) and
the per-request LLM gateway factory deliberately stay in ``api.py`` so the existing monkeypatch
seams (``owcopilot.service.api._is_loopback`` / ``.load_dotenv``) keep resolving through that
module namespace at call time.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import Header, HTTPException, Request
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..app.workspaces import sanitize_world_name, worlds_home
from ..config import DEFAULT_LLM_MODE
from ..content.models import ContentBundle
from ..graph.index import build_content_graph
from ..llm.cache import build_cache_backend
from ..pipeline.project import ProjectContext
from ..platform import (
    AuthError,
    Principal,
    Role,
    require_role,
    resolve_principal,
)
from ..retrieval.bm25 import BM25Retriever
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.embedding import resolve_embedder
from ..retrieval.graph_expand import GraphExpansionRetriever
from ..retrieval.neural_rerank import reranker_for_corpus
from ..retrieval.vector import VectorRetriever
from ..storage import SQLiteStore
from ..telemetry import deterministic_step, summarize_workflow


# --------------------------------------------------------------------------- settings (env-driven)
def _llm_mode() -> str:
    """`real` (OpenAI-compatible provider) or `offline` (deterministic doubles).

    `offline` keeps startup resilient with no provider configured — deterministic features
    (audit / impact / export / retrieval) work and AI features fail closed — but the doubles
    themselves only run when `OWCOPILOT_ALLOW_OFFLINE_LLM` is set (a test/CI fixture, never a
    shipped product mode). Read per request so the mode can be flipped by env without restarting.
    """
    return os.getenv("OWCOPILOT_LLM_MODE", DEFAULT_LLM_MODE).strip().lower()


def _cache_mode() -> str:
    return os.getenv("OWCOPILOT_CACHE_MODE", "exact+semantic").strip().lower()


def _semantic_threshold() -> float:
    return float(os.getenv("OWCOPILOT_SEMANTIC_THRESHOLD", "0.9"))


def _redis_url() -> str:
    return os.getenv("OWCOPILOT_REDIS_URL", "redis://127.0.0.1:6379/0")


def _rate_limit_backend() -> str:
    backend = os.getenv("OWCOPILOT_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend not in {"memory", "redis"}:
        raise RuntimeError(f"unsupported OWCOPILOT_RATE_LIMIT_BACKEND {backend!r}")
    return backend


def _build_cache():
    return build_cache_backend(
        _cache_mode(),
        semantic_threshold=_semantic_threshold(),
        embedder=resolve_embedder(),
        redis_url=_redis_url(),
    )


# --------------------------------------------------------------------------- access control
def _require_api_key(x_api_key: str | None) -> None:
    """Opt-in API-key gate. If `OWCOPILOT_API_KEY` is set it is enforced; if unset the API is open
    (dev/offline). Production with the real model MUST set it — the API spends real money (A4)."""
    expected = os.getenv("OWCOPILOT_API_KEY")
    if expected and x_api_key != expected:
        raise HTTPException(status_code=401, detail="missing or invalid X-API-Key")


class _MemoryRateLimiter:
    """Per-client fixed-window limiter (single process). Keyed by API key, else client IP.

    This caps cost-bleed from a single caller on one instance. It is deliberately in-memory and
    NOT shared across replicas — behind multiple instances, enforce quotas at an API gateway / a
    shared store (Redis), exactly as the L1 cache would move to Redis (A4). `per_min <= 0` disables
    it (handy for load tests).
    """

    def __init__(self, per_min: int):
        self.per_min = per_min
        self._hits: dict[str, deque[float]] = {}
        self._lock = Lock()

    def check(self, key: str) -> None:
        if self.per_min <= 0:
            return
        now = time.monotonic()
        with self._lock:
            dq = self._hits.setdefault(key, deque())
            while dq and now - dq[0] > 60.0:
                dq.popleft()
            if len(dq) >= self.per_min:
                raise HTTPException(status_code=429, detail="rate limit exceeded; retry shortly")
            dq.append(now)


class _RedisRateLimiter:
    """Fixed-window Redis limiter for multi-instance deploys."""

    def __init__(
        self,
        per_min: int,
        *,
        url: str = "redis://127.0.0.1:6379/0",
        client: Any | None = None,
        prefix: str = "owcopilot:rl:",
    ):
        self.per_min = per_min
        self.url = url
        self._client = client
        self.prefix = prefix

    def check(self, key: str) -> None:
        if self.per_min <= 0:
            return
        now_bucket = int(time.time() // 60)
        safe_key = hashlib.sha256(key.encode("utf-8")).hexdigest()
        redis_key = f"{self.prefix}{now_bucket}:{safe_key}"
        try:
            count = int(self._conn().incr(redis_key))
            if count == 1:
                self._conn().expire(redis_key, 70)
        except Exception as e:
            raise HTTPException(status_code=503, detail="redis rate limiter unavailable") from e
        if count > self.per_min:
            raise HTTPException(status_code=429, detail="rate limit exceeded; retry shortly")

    def _conn(self):
        if self._client is None:
            import redis

            self._client = redis.Redis.from_url(self.url, decode_responses=True)
        return self._client


def _build_rate_limiter(per_min: int):
    if _rate_limit_backend() == "redis":
        return _RedisRateLimiter(per_min, url=_redis_url())
    return _MemoryRateLimiter(per_min)


def _client_key(x_api_key: str | None, request: Request) -> str:
    return x_api_key or (request.client.host if request.client else "anonymous")


# Routes served without the API-key / rate-limit gate: liveness, the API-hint root, and the auto
# docs. The SPA is a mounted sub-app, which FastAPI dependencies never reach, so its assets need no
# entry here — only routes declared on the app itself pass through ``_require_client``.
_PUBLIC_PATHS = frozenset(
    {"/", "/health", "/docs", "/redoc", "/openapi.json", "/docs/oauth2-redirect"}
)


def _require_client(
    request: Request, x_api_key: str | None = Header(default=None, alias="X-API-Key")
) -> None:
    """The single auth + rate-limit gate for every API route, wired as a global app dependency so a
    new endpoint can't forget it (``test_every_protected_route_requires_auth`` proves it). Runs
    during dependency solving, i.e. before body validation, so an unauthenticated call is a clean
    401 rather than a 422. The rate limiter is read from ``app.state`` (set per ``create_app``)."""
    path = request.url.path
    # /platform/* runs its own principal-based auth (bearer JWT OR the API key), so the api-key gate
    # here would 401 a valid bearer caller before it ever reaches that logic. Those routes still
    # reject unauthenticated callers via resolve_principal.
    if path in _PUBLIC_PATHS or path.startswith("/platform/"):
        return
    # api-key check first so an unauthenticated caller 401s without consuming rate-limit budget.
    _require_api_key(x_api_key)
    request.app.state.limiter.check(_client_key(x_api_key, request))


def _context_builder_for_bundle(bundle: ContentBundle) -> tuple[SQLiteStore, ContextPackBuilder]:
    store = SQLiteStore()
    graph = build_content_graph(bundle)
    store.replace_content_index(bundle)
    store.replace_graph_edges(graph)
    return store, ContextPackBuilder(
        bm25=BM25Retriever(store),
        vector=VectorRetriever(store, auto_tier=True),
        graph=GraphExpansionRetriever(graph),
        neural_reranker=reranker_for_corpus(len(graph.node_refs())),
    )


# Managed world names double as project ids and are frequently CJK, so the rule is
# "no path-hostile characters", not "ASCII only". Traversal is impossible by charset.
PROJECT_ID_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _validate_project_id(project: str) -> None:
    if (
        not project
        or len(project) > 64
        or project.strip(".") == ""
        or PROJECT_ID_FORBIDDEN_RE.search(project)
    ):
        raise HTTPException(status_code=400, detail="invalid project id")


def _project_registry() -> dict[str, Path]:
    raw = os.getenv("OWCOPILOT_PROJECTS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail="invalid OWCOPILOT_PROJECTS_JSON") from e
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=500, detail="OWCOPILOT_PROJECTS_JSON must be an object")

    registry: dict[str, Path] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise HTTPException(
                status_code=500,
                detail="OWCOPILOT_PROJECTS_JSON entries must map string ids to string paths",
            )
        registry[key] = Path(value).expanduser().resolve()
    return registry


def _project_content_root(project: str) -> Path | None:
    _validate_project_id(project)
    root = _project_registry().get(project)
    if root is None:
        # Zero-config path: a managed world's NAME doubles as its project id, so a fresh
        # install needs no OWCOPILOT_PROJECTS_JSON at all — create a world, use its name.
        try:
            managed = worlds_home() / sanitize_world_name(project)
        except ValueError:
            return None
        if managed.exists() and managed.is_dir():
            return managed
        return None
    if not root.exists() or not root.is_dir():
        raise HTTPException(status_code=404, detail=f"configured project {project!r} not found")
    return root


def _project_sqlite_path(project: str, content_root: Path) -> Path:
    runtime_dir = os.getenv("OWCOPILOT_RUNTIME_DIR")
    if runtime_dir:
        base = Path(runtime_dir).expanduser().resolve()
        base.mkdir(parents=True, exist_ok=True)
        suffix = hashlib.sha256(str(content_root).encode("utf-8")).hexdigest()[:12]
        return base / f"{project}-{suffix}.sqlite"

    local_runtime = content_root / ".owcopilot"
    local_runtime.mkdir(parents=True, exist_ok=True)
    return local_runtime / "runtime.sqlite"


def _open_project_context(project: str) -> ProjectContext | None:
    content_root = _project_content_root(project)
    if content_root is None:
        return None
    return ProjectContext.open(
        content_root,
        sqlite_path=_project_sqlite_path(project, content_root),
    )


def _registered_project(project: str) -> ProjectContext:
    project_context = _open_project_context(project)
    if project_context is None:
        raise HTTPException(
            status_code=404,
            detail=(f"project {project!r} is not registered; configure OWCOPILOT_PROJECTS_JSON"),
        )
    return project_context


def _project_root_or_404(project: str) -> str:
    content_root = _project_content_root(project)
    if content_root is None:
        raise HTTPException(
            status_code=404,
            detail=f"project {project!r} is not registered (OWCOPILOT_PROJECTS_JSON)",
        )
    return str(content_root)


def _manage_error(e: ValueError) -> HTTPException:
    status = 404 if "不存在" in str(e) else 409
    return HTTPException(status_code=status, detail=str(e))


def _filter_issue_dicts(
    issues: list[dict[str, Any]],
    *,
    severity: str | None = None,
    rule_code: str | None = None,
    status: str | None = None,
) -> list[dict[str, Any]]:
    filtered = issues
    if severity is not None:
        filtered = [issue for issue in filtered if issue.get("severity") == severity]
    if rule_code is not None:
        filtered = [issue for issue in filtered if issue.get("rule_code") == rule_code]
    if status is not None:
        filtered = [issue for issue in filtered if issue.get("status") == status]
    return filtered


def _deterministic_cost_budget(step_name: str) -> dict[str, Any]:
    return summarize_workflow([deterministic_step(step_name)]).budget.model_dump(mode="json")


# --------------------------------------------------------------------------- tenancy principal
def _principal(authorization: str | None, x_api_key: str | None) -> Principal:
    try:
        return resolve_principal(
            authorization=authorization,
            x_api_key=x_api_key,
            expected_api_key=os.getenv("OWCOPILOT_API_KEY"),
        )
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc


def _require_role_http(principal: Principal, minimum: Role) -> None:
    try:
        require_role(principal, minimum)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


# --------------------------------------------------------------------------- frontend (SPA) assets
def _frontend_dist_dir() -> Path | None:
    """Locate the built Vue app: explicit env first, then the repo-relative default."""
    override = os.getenv("OWCOPILOT_FRONTEND_DIST", "").strip()
    candidates = (
        [Path(override)]
        if override
        else [
            Path(__file__).resolve().parents[3] / "frontend" / "dist",
            Path.cwd() / "frontend" / "dist",
        ]
    )
    for candidate in candidates:
        if (candidate / "index.html").exists():
            return candidate
    return None


class _SpaStaticFiles(StaticFiles):
    """Serve the built frontend; unknown paths fall back to index.html so client-side
    routes (/overview, /review …) survive a hard refresh."""

    async def get_response(self, path: str, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as e:
            if e.status_code == 404:
                return await super().get_response("index.html", scope)
            raise
