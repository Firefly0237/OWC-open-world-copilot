"""FastAPI service: the v2 content-workbench API plus the legacy quest endpoints.

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
providers) for CI and the docker smoke test. Real mode is fail-closed twice over: the legacy
global `OWCOPILOT_LLM_MODE=real` requires full provider + API-key config at startup, and the
per-request `llm_mode=real` on v2 endpoints refuses unless `OWCOPILOT_API_KEY` gates the service.

The legacy `POST /quests:generate` / `:batch_generate` endpoints (intent + World Bible -> a
validated, repaired Quest) are kept for compatibility with earlier demos and the docker smoke.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import time
import urllib.parse
import uuid
from collections import deque
from collections.abc import Callable
from pathlib import Path
from threading import Lock
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from ..app.actions import (
    decide_review_action,
    delete_object_action,
    list_review_items_action,
    run_extraction_action,
    run_theme_sweep_action,
    run_world_seed_action,
    update_entity_action,
)
from ..app.view_models import build_content_inventory, build_project_overview
from ..app.workspaces import (
    create_managed_world,
    export_world_zip,
    import_world_zip,
    list_managed_worlds,
    sanitize_world_name,
    worlds_home,
)
from ..assembly import PrefixMode, RouterMode, build_grounded_pipeline
from ..assist.barks import BarkBatchService
from ..assist.dialogue_trees import DialogueTreeService, OfflineDialogueTreeProvider
from ..assist.drafts import QuestDraftService
from ..assist.flavor import FlavorBatchService, OfflineFlavorProvider
from ..assist.offline import OfflineBarksProvider, OfflineQuestDraftProvider
from ..assist.review_queue import ReviewQueue
from ..audit.baseline import issue_fingerprint
from ..audit.context import AuditContext
from ..audit.default_rules import build_default_rule_registry
from ..audit.runner import AuditRunner
from ..content.hash import content_hash
from ..content.models import ContentBundle
from ..evaluation.quality import evaluate_quest_quality
from ..exporters import EngineTarget, export_content_bundle
from ..extraction import (
    ExtractionDraft,
    ExtractionService,
    OfflineExtractionProvider,
    apply_gap_answers,
    quests_from_beats,
)
from ..graph.index import build_content_graph
from ..impact import Change, ChangeSet, ChangeType, ImpactAnalyzer, ImpactLevel
from ..llm.cache import build_cache_backend
from ..llm.gateway import LLMGateway, LLMGatewayError, OpenAICompatProvider, StructuredFakeProvider
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector
from ..pipeline.audit import run_full_audit
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.project import ProjectContext
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..retrieval.bm25 import BM25Retriever
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.graph_expand import GraphExpansionRetriever
from ..retrieval.vector import VectorRetriever
from ..storage import SQLiteStore
from ..telemetry import deterministic_step, llm_step, summarize_workflow
from ..trust.security import PathSecurityError, resolve_under_root
from ..util import load_dotenv
from ..worldbible.ingest import parse_worldbible_md
from ..worldbible.models import WorldBible, world_bible_hash
from ..worldbible.security import (
    WorldBibleLimits,
    WorldBibleSecurityError,
    validate_world_bible_model,
    validate_world_bible_text,
)
from .jobs import JobManager

__version__ = "0.2.0"


# --------------------------------------------------------------------------- settings (env-driven)
def _llm_mode() -> str:
    """`offline` (default, fake provider, $0) or `real` (OpenAI-compatible provider).

    Read per request so the mode can be flipped by env without restarting the import.
    """
    return os.getenv("OWCOPILOT_LLM_MODE", "offline").strip().lower()


def _router_mode() -> RouterMode:
    mode = os.getenv("OWCOPILOT_ROUTER_MODE", "cascade").strip().lower()
    if mode == "static":
        return "static"
    if mode == "cascade":
        return "cascade"
    raise RuntimeError(f"unsupported OWCOPILOT_ROUTER_MODE {mode!r}")


def _cache_mode() -> str:
    return os.getenv("OWCOPILOT_CACHE_MODE", "exact+semantic").strip().lower()


def _prefix_mode() -> PrefixMode:
    mode = os.getenv("OWCOPILOT_PREFIX_MODE", "retrieval").strip().lower()
    if mode == "retrieval":
        return "retrieval"
    if mode == "stable":
        return "stable"
    raise RuntimeError(f"unsupported OWCOPILOT_PREFIX_MODE {mode!r}")


def _semantic_threshold() -> float:
    return float(os.getenv("OWCOPILOT_SEMANTIC_THRESHOLD", "0.9"))


def _redis_url() -> str:
    return os.getenv("OWCOPILOT_REDIS_URL", "redis://127.0.0.1:6379/0")


def _cheap_model() -> str:
    return os.getenv("OWCOPILOT_CHEAP_MODEL", "deepseek-v4-flash")


def _frontier_model() -> str:
    return os.getenv("OWCOPILOT_FRONTIER_MODEL", "deepseek-v4-pro")


def _llm_max_retries() -> int:
    return int(os.getenv("OWCOPILOT_LLM_MAX_RETRIES", "2"))


def _llm_retry_backoff_seconds() -> float:
    return float(os.getenv("OWCOPILOT_LLM_RETRY_BACKOFF_SEC", "0.25"))


def _rate_limit_backend() -> str:
    backend = os.getenv("OWCOPILOT_RATE_LIMIT_BACKEND", "memory").strip().lower()
    if backend not in {"memory", "redis"}:
        raise RuntimeError(f"unsupported OWCOPILOT_RATE_LIMIT_BACKEND {backend!r}")
    return backend


def _world_bible_limits() -> WorldBibleLimits:
    return WorldBibleLimits(
        max_chars=int(os.getenv("OWCOPILOT_MAX_WORLDBIBLE_CHARS", "200000")),
        max_entities=int(os.getenv("OWCOPILOT_MAX_WORLDBIBLE_ENTITIES", "500")),
        max_relations=int(os.getenv("OWCOPILOT_MAX_WORLDBIBLE_RELATIONS", "2000")),
        max_field_chars=int(os.getenv("OWCOPILOT_MAX_WORLDBIBLE_FIELD_CHARS", "2000")),
    )


def _require_real_mode_config() -> None:
    """Fail closed before the service can spend money with unsafe real-mode settings."""
    load_dotenv()
    required = ("OPENAI_BASE_URL", "OPENAI_API_KEY", "OWCOPILOT_API_KEY")
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "OWCOPILOT_LLM_MODE=real requires these environment variables: " + ", ".join(missing)
        )


def _build_providers():
    """Provider pair for the service.

    Offline: structured fake on both tiers so static *and* cascade generation return parseable Quest
    JSON. Real: cheap/strong OpenAI-compatible providers for the service's deployable surface.
    """
    if _llm_mode() != "real":
        fake = StructuredFakeProvider()
        return fake, fake

    _require_real_mode_config()
    return (
        OpenAICompatProvider(model=_cheap_model()),
        OpenAICompatProvider(model=_frontier_model()),
    )


def _build_cache():
    return build_cache_backend(
        _cache_mode(),
        semantic_threshold=_semantic_threshold(),
        redis_url=_redis_url(),
    )


# --------------------------------------------------------------------------- request / response
class GenerateOptions(BaseModel):
    max_repair_attempts: int = Field(default=2, ge=0, le=5)
    include_trace: bool = False


class GenerateRequest(BaseModel):
    intent: str = Field(min_length=1, max_length=4000)
    world_bible_md: str | None = Field(default=None, max_length=200_000)
    world_bible_id: str | None = Field(
        default=None,
        description="Reserved for a future project registry; inline `world_bible_md` is required.",
    )
    options: GenerateOptions = Field(default_factory=GenerateOptions)


class BatchGenerateRequest(BaseModel):
    intents: list[str] = Field(min_length=1, max_length=50)
    world_bible_md: str | None = Field(default=None, max_length=200_000)
    world_bible_id: str | None = Field(
        default=None,
        description="Reserved for a future project registry; inline `world_bible_md` is required.",
    )
    options: GenerateOptions = Field(default_factory=GenerateOptions)

    @field_validator("intents")
    @classmethod
    def _validate_intents(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("all intents must be non-empty")
        if any(len(item) > 4000 for item in value):
            raise ValueError("each intent must be <= 4000 characters")
        return value


class GenerateResponse(BaseModel):
    request_id: str
    quest: dict[str, Any]
    issues: list[dict[str, Any]]
    consistent: bool
    repaired: bool
    repair_attempts: int
    review_status: str = "pending_review"
    quality: dict[str, Any]
    telemetry: dict[str, Any]
    llm_mode: str
    world_bible_hash: str
    input_warnings: list[str] = Field(default_factory=list)
    trace: dict[str, Any] | None = None


class BatchGenerateResponse(BaseModel):
    request_id: str
    items: list[GenerateResponse]
    telemetry: dict[str, Any]
    llm_mode: str
    world_bible_hash: str
    input_warnings: list[str] = Field(default_factory=list)


class ProjectContentRequest(BaseModel):
    content: ContentBundle | None = None


class ProjectAuditRequest(ProjectContentRequest):
    persist: bool = True


class ProjectAuditResponse(BaseModel):
    request_id: str
    project: str
    content_hash: str
    audit_run: dict[str, Any]
    issues: list[dict[str, Any]]
    totals: dict[str, int]
    cost_budget: dict[str, Any]


class ProjectIssuesResponse(BaseModel):
    project: str
    issues: list[dict[str, Any]]
    cost_budget: dict[str, Any]


class ProjectContextPackRequest(ProjectContentRequest):
    query: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)


class ProjectContextPackResponse(BaseModel):
    request_id: str
    project: str
    refs: list[str]
    hits: list[dict[str, Any]]
    cost_budget: dict[str, Any]


class ProjectAskRequest(ProjectContentRequest):
    query: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)
    max_cost_usd: float | None = Field(default=None, ge=0)


class ProjectAskResponse(BaseModel):
    request_id: str
    project: str
    answer: dict[str, Any]
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ImpactChangeSpec(BaseModel):
    change_type: str
    target_ref: str = Field(min_length=1, max_length=400)


class ProjectImpactRequest(BaseModel):
    changes: list[ImpactChangeSpec] = Field(min_length=1, max_length=50)
    max_depth: int = Field(default=2, ge=1, le=4)


class ProjectImpactResponse(BaseModel):
    request_id: str
    project: str
    must_change: list[dict[str, Any]]
    suggest_check: list[dict[str, Any]]
    total: int
    cost_budget: dict[str, Any]


class _LLMModeRequest(BaseModel):
    llm_mode: str = Field(default="offline", pattern="^(offline|real)$")
    llm_model: str | None = None
    max_cost_usd: float | None = Field(default=None, ge=0)


class ProjectSuggestRequest(_LLMModeRequest):
    max_candidates: int = Field(default=3, ge=1, le=5)
    budget_tokens: int = Field(default=600, ge=1, le=8000)


class ProjectSuggestResponse(BaseModel):
    request_id: str
    project: str
    issue_id: str
    candidates: list[dict[str, Any]]
    rejected_count: int
    parse_failed: bool
    used_llm: bool
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectPatchDecisionRequest(BaseModel):
    operator: str = Field(min_length=1, max_length=200)


class ProjectPatchApplyResponse(BaseModel):
    request_id: str
    project: str
    applied: bool
    patch_id: str
    reason: str = ""
    introduced_errors: list[str] = Field(default_factory=list)
    resolved_errors: list[str] = Field(default_factory=list)
    post_audit_open_errors: int = 0
    cost_budget: dict[str, Any]


class ProjectPatchRollbackResponse(BaseModel):
    request_id: str
    project: str
    rolled_back: bool
    patch_id: str
    post_audit_open_errors: int = 0
    cost_budget: dict[str, Any]


class ProjectExtractionRunRequest(_LLMModeRequest):
    title: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=400_000)
    source_kind: str = Field(default="文稿", max_length=40)
    max_chunks: int = Field(default=12, ge=1, le=24)


class ProjectExtractionRunResponse(BaseModel):
    request_id: str
    project: str
    draft: dict[str, Any]
    stats: dict[str, int]
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectExtractionSubmitRequest(BaseModel):
    draft: dict[str, Any]
    answers: dict[str, str] = Field(default_factory=dict)
    include_beats_as_quests: bool = False


class ProjectExtractionSubmitResponse(BaseModel):
    request_id: str
    project: str
    review_item_id: str
    open_gaps: int
    issues: list[dict[str, Any]]


class ProjectDialogueTreeRequest(_LLMModeRequest):
    participant_ids: list[str] = Field(min_length=1, max_length=6)
    brief: str = Field(min_length=1, max_length=2000)
    quest_id: str | None = None
    max_nodes: int = Field(default=12, ge=4, le=24)
    max_chars: int = Field(default=120, ge=20, le=400)


class ProjectDialogueTreeResponse(BaseModel):
    request_id: str
    project: str
    tree: dict[str, Any]
    lint_issues: list[dict[str, Any]]
    structure_problems: list[str]
    review_item_id: str | None
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectFlavorRequest(_LLMModeRequest):
    category: str = Field(pattern="^(item|skill|achievement)$")
    names: list[str] = Field(min_length=1, max_length=50)
    theme: str = Field(default="", max_length=200)
    max_chars: int = Field(default=120, ge=20, le=400)


class ProjectFlavorResponse(BaseModel):
    request_id: str
    project: str
    batch_id: str
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    review_item_id: str | None
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectDraftRequest(_LLMModeRequest):
    brief: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)


class ProjectDraftResponse(BaseModel):
    request_id: str
    project: str
    quest: dict[str, Any]
    issues: list[dict[str, Any]]
    review_item_id: str
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectBarksRequest(_LLMModeRequest):
    speaker_ids: list[str] = Field(min_length=1, max_length=50)
    topic: str = Field(min_length=1, max_length=1000)
    variants_per_speaker: int = Field(default=4, ge=1, le=10)
    max_chars: int = Field(default=40, ge=8, le=500)
    allowed_entity_ids: list[str] = Field(default_factory=list)


class ProjectBarksResponse(BaseModel):
    request_id: str
    project: str
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    review_item_ids: list[str]
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectExportRequest(BaseModel):
    target_engine: str = Field(default=EngineTarget.GENERIC.value)


class ProjectExportResponse(BaseModel):
    request_id: str
    project: str
    output_dir: str
    manifest: dict[str, Any]
    cost_budget: dict[str, Any]


# ---- round-13 platform endpoints: workspaces / review decisions / sweep / manage ----
class WorkspaceInfo(BaseModel):
    name: str
    path: str


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceInfo]


class WorkspaceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)


class WorkspaceImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    zip_base64: str = Field(min_length=4)


class ReviewItemsResponse(BaseModel):
    project: str
    count: int
    items: list[dict[str, Any]]
    cost_budget: dict[str, Any]


class ReviewDecideRequest(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    operator: str = Field(min_length=1, max_length=80)


class ReviewDecideResponse(BaseModel):
    project: str
    item_id: str
    decision: str
    written_ref: str | None = None
    post_audit_open_errors: int = 0
    cost_budget: dict[str, Any]


class ProjectSweepRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=200)
    extra_terms: list[str] = Field(default_factory=list)
    use_llm: bool = False
    llm_mode: str = "offline"
    llm_model: str = "deepseek-v4-flash"
    max_judge: int = Field(default=400, ge=1, le=2000)


class ProjectSweepResponse(BaseModel):
    project: str
    theme: str
    scanned_total: int
    scanned_by_kind: dict[str, int]
    llm_used: bool
    judged_count: int
    judge_skipped: int
    hits: list[dict[str, Any]]
    review_suggested: list[dict[str, Any]]
    markdown: str
    cost_budget: dict[str, Any]


_JOB_KINDS = ("world_seed", "extraction", "theme_sweep")
_JOB_PARAM_KEYS: dict[str, set[str]] = {
    "world_seed": {"brief", "llm_mode", "llm_model", "budget_tokens"},
    "extraction": {"title", "text", "source_kind", "max_chunks", "llm_mode", "llm_model"},
    "theme_sweep": {"theme", "extra_terms", "use_llm", "llm_mode", "llm_model", "max_judge"},
}


class JobCreateRequest(BaseModel):
    kind: str = Field(pattern="^(world_seed|extraction|theme_sweep)$")
    params: dict[str, Any] = Field(default_factory=dict)


class JobCreatedResponse(BaseModel):
    job_id: str
    kind: str
    status: str


class JobStatusResponse(BaseModel):
    job_id: str
    kind: str
    status: str
    events: list[dict[str, Any]]
    result: dict[str, Any] | None = None
    error: str | None = None


class EntityUpdateRequest(BaseModel):
    name: str | None = None
    description: str | None = None
    tags: list[str] | None = None


class EntityUpdateResponse(BaseModel):
    project: str
    entity: dict[str, Any]
    changed: list[str]
    cost_budget: dict[str, Any]


class ObjectDeleteResponse(BaseModel):
    project: str
    deleted_ref: str
    removed_relations: int
    post_audit_open_errors: int
    cost_budget: dict[str, Any]


def _resolve_world_bible(
    req: GenerateRequest | BatchGenerateRequest,
) -> tuple[WorldBible, list[str]]:
    """Resolve the caller-provided World Bible.

    The service intentionally has no bundled sample world. Real usage should pass the current
    project's World Bible explicitly (today as markdown; a project registry can fill
    `world_bible_id` later).
    """
    limits = _world_bible_limits()
    warnings: list[str] = []
    if req.world_bible_md:
        try:
            warnings = validate_world_bible_text(req.world_bible_md, limits)
            wb = parse_worldbible_md(req.world_bible_md)
            validate_world_bible_model(wb, limits)
            return wb, warnings
        except WorldBibleSecurityError as e:
            raise HTTPException(status_code=413, detail=str(e)) from e
    if req.world_bible_id:
        raise HTTPException(
            status_code=404,
            detail=(
                f"world_bible_id {req.world_bible_id!r} is not configured; send "
                "`world_bible_md` inline."
            ),
        )
    raise HTTPException(
        status_code=400,
        detail="world_bible_md is required; this service does not bundle a sample World Bible.",
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


def _trace_for(final: dict[str, Any]) -> dict[str, Any]:
    phase = final.get("phase")
    return {
        "phase": getattr(phase, "value", phase),
        "plan": final.get("plan", []),
        "log": final.get("log", []),
        "repair_attempts": final.get("repair_attempts", 0),
        "max_repair_attempts": final.get("max_repair_attempts"),
    }


def _response_from_final(
    *,
    request_id: str,
    final: dict[str, Any],
    telemetry: TelemetryCollector,
    llm_mode: str,
    wb_hash: str,
    input_warnings: list[str],
    include_trace: bool,
) -> GenerateResponse:
    issues_obj = final.get("issues", [])
    issues = [i.model_dump() for i in issues_obj]
    errors = [i for i in issues if i.get("severity") == "error"]
    artifact = final.get("artifact") or {}
    quality = evaluate_quest_quality(artifact, issues_obj).model_dump()
    return GenerateResponse(
        request_id=request_id,
        quest=artifact,
        issues=issues,
        consistent=not errors,
        repaired=final.get("repair_attempts", 0) > 0,
        repair_attempts=final.get("repair_attempts", 0),
        review_status="pending_review",
        quality=quality,
        telemetry=telemetry.summary(),
        llm_mode=llm_mode,
        world_bible_hash=wb_hash,
        input_warnings=input_warnings,
        trace=_trace_for(final) if include_trace else None,
    )


def _context_builder_for_bundle(bundle: ContentBundle) -> tuple[SQLiteStore, ContextPackBuilder]:
    store = SQLiteStore()
    graph = build_content_graph(bundle)
    store.replace_content_index(bundle)
    store.replace_graph_edges(graph)
    return store, ContextPackBuilder(
        bm25=BM25Retriever(store),
        vector=VectorRetriever(store),
        graph=GraphExpansionRetriever(graph),
    )


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


def _validate_project_id(project: str) -> None:
    if not PROJECT_ID_RE.fullmatch(project):
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


# --------------------------------------------------------------------------- app factory
def create_app() -> FastAPI:
    llm_mode = _llm_mode()
    router_mode = _router_mode()
    prefix_mode = _prefix_mode()
    service_cache = _build_cache()
    cheap_provider, frontier_provider = _build_providers()

    app = FastAPI(
        title="owcopilot",
        version=__version__,
        description="Consistency-checked quest generation for open-world game development.",
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
    jobs_manager = JobManager()
    app.state.v2_issues = {}

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "llm_mode": llm_mode}

    @app.post("/quests:generate", response_model=GenerateResponse)
    def generate_quest(
        req: GenerateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> GenerateResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))

        wb, input_warnings = _resolve_world_bible(req)
        wb_hash = world_bible_hash(wb)
        graph, telemetry, _generator = build_grounded_pipeline(
            wb,
            cheap_provider=cheap_provider,
            frontier_provider=frontier_provider,
            use_llm_repair=(llm_mode == "real"),
            router_mode=router_mode,
            cache=service_cache,
            prefix_mode=prefix_mode,
            land=False,  # engine landing is the caller's local step, not a web endpoint (A0)
            llm_max_retries=_llm_max_retries(),
            llm_retry_backoff_seconds=_llm_retry_backoff_seconds(),
        )
        try:
            final = graph.invoke(
                {
                    "intent": req.intent,
                    "max_repair_attempts": req.options.max_repair_attempts,
                    "log": [],
                }
            )
        except LLMGatewayError as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "request_id": request_id,
                    "message": "generation failed",
                    "category": e.category,
                    "task": e.task,
                    "tier": e.tier,
                    "attempts": e.attempts,
                },
            ) from e
        except Exception as e:  # e.g. real model returns no parseable JSON even after a retry
            raise HTTPException(
                status_code=502,
                detail={"request_id": request_id, "message": f"generation failed: {e}"},
            ) from e

        return _response_from_final(
            request_id=request_id,
            final=final,
            telemetry=telemetry,
            llm_mode=llm_mode,
            wb_hash=wb_hash,
            input_warnings=input_warnings,
            include_trace=req.options.include_trace,
        )

    @app.post("/quests:batch_generate", response_model=BatchGenerateResponse)
    def batch_generate_quests(
        req: BatchGenerateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> BatchGenerateResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))

        wb, input_warnings = _resolve_world_bible(req)
        wb_hash = world_bible_hash(wb)
        graph, telemetry, _generator = build_grounded_pipeline(
            wb,
            cheap_provider=cheap_provider,
            frontier_provider=frontier_provider,
            use_llm_repair=(llm_mode == "real"),
            router_mode=router_mode,
            cache=service_cache,
            prefix_mode=prefix_mode,
            land=False,
            llm_max_retries=_llm_max_retries(),
            llm_retry_backoff_seconds=_llm_retry_backoff_seconds(),
        )
        items: list[GenerateResponse] = []
        try:
            for intent in req.intents:
                start = len(telemetry.records)
                final = graph.invoke(
                    {
                        "intent": intent,
                        "max_repair_attempts": req.options.max_repair_attempts,
                        "log": [],
                    }
                )
                item_telemetry = TelemetryCollector(records=list(telemetry.records[start:]))
                items.append(
                    _response_from_final(
                        request_id=request_id,
                        final=final,
                        telemetry=item_telemetry,
                        llm_mode=llm_mode,
                        wb_hash=wb_hash,
                        input_warnings=input_warnings,
                        include_trace=req.options.include_trace,
                    )
                )
        except LLMGatewayError as e:
            raise HTTPException(
                status_code=502,
                detail={
                    "request_id": request_id,
                    "message": "batch generation failed",
                    "category": e.category,
                    "task": e.task,
                    "tier": e.tier,
                    "attempts": e.attempts,
                },
            ) from e
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail={"request_id": request_id, "message": f"batch generation failed: {e}"},
            ) from e

        return BatchGenerateResponse(
            request_id=request_id,
            items=items,
            telemetry=telemetry.summary(),
            llm_mode=llm_mode,
            world_bible_hash=wb_hash,
            input_warnings=input_warnings,
        )

    @app.post("/projects/{project}/audits", response_model=ProjectAuditResponse)
    def create_project_audit(
        project: str,
        req: ProjectAuditRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectAuditResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))

        if req.content is None:
            project_context = _open_project_context(project)
            if project_context is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"project {project!r} is not registered; configure "
                        "OWCOPILOT_PROJECTS_JSON or send inline content"
                    ),
                )
            try:
                result = run_full_audit(project_context, persist=req.persist)
                return ProjectAuditResponse(
                    request_id=request_id,
                    project=project,
                    content_hash=content_hash(project_context.bundle),
                    audit_run=result.run.model_dump(mode="json"),
                    issues=[issue.model_dump(mode="json") for issue in result.issues],
                    totals=result.run.totals,
                    cost_budget=_deterministic_cost_budget("audit_project"),
                )
            finally:
                project_context.close()

        runner = AuditRunner(build_default_rule_registry())
        result = runner.run(AuditContext.from_bundle(req.content))
        issues = [issue.model_dump(mode="json") for issue in result.issues]
        if req.persist:
            app.state.v2_issues[project] = issues
        return ProjectAuditResponse(
            request_id=request_id,
            project=project,
            content_hash=content_hash(req.content),
            audit_run=result.run.model_dump(mode="json"),
            issues=issues,
            totals=result.run.totals,
            cost_budget=_deterministic_cost_budget("audit_project"),
        )

    @app.get("/projects/{project}/issues", response_model=ProjectIssuesResponse)
    def list_project_issues(
        project: str,
        request: Request,
        severity: str | None = None,
        rule_code: str | None = None,
        status: str | None = None,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectIssuesResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _open_project_context(project)
        if project_context is not None:
            try:
                return ProjectIssuesResponse(
                    project=project,
                    issues=[
                        issue.model_dump(mode="json")
                        for issue in project_context.sqlite_store.list_issues(
                            severity=severity,
                            rule_code=rule_code,
                            status=status,
                        )
                    ],
                    cost_budget=_deterministic_cost_budget("list_issues"),
                )
            finally:
                project_context.close()

        return ProjectIssuesResponse(
            project=project,
            issues=_filter_issue_dicts(
                app.state.v2_issues.get(project, []),
                severity=severity,
                rule_code=rule_code,
                status=status,
            ),
            cost_budget=_deterministic_cost_budget("list_issues"),
        )

    @app.post("/projects/{project}/context:pack", response_model=ProjectContextPackResponse)
    def create_context_pack(
        project: str,
        req: ProjectContextPackRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectContextPackResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        if req.content is None:
            project_context = _open_project_context(project)
            if project_context is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"project {project!r} is not registered; configure "
                        "OWCOPILOT_PROJECTS_JSON or send inline content"
                    ),
                )
            try:
                pack = project_context.context_builder.build(
                    req.query,
                    budget_tokens=req.budget_tokens,
                )
                return ProjectContextPackResponse(
                    request_id=request_id,
                    project=project,
                    refs=pack.refs,
                    hits=[hit.model_dump(mode="json") for hit in pack.hits],
                    cost_budget=_deterministic_cost_budget("build_context_pack"),
                )
            finally:
                project_context.close()

        store, builder = _context_builder_for_bundle(req.content)
        try:
            pack = builder.build(req.query, budget_tokens=req.budget_tokens)
            return ProjectContextPackResponse(
                request_id=request_id,
                project=project,
                refs=pack.refs,
                hits=[hit.model_dump(mode="json") for hit in pack.hits],
                cost_budget=_deterministic_cost_budget("build_context_pack"),
            )
        finally:
            store.close()

    @app.post("/projects/{project}/ask", response_model=ProjectAskResponse)
    def ask_project(
        project: str,
        req: ProjectAskRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectAskResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = None
        store: SQLiteStore | None = None
        if req.content is None:
            project_context = _open_project_context(project)
            if project_context is None:
                raise HTTPException(
                    status_code=404,
                    detail=(
                        f"project {project!r} is not registered; configure "
                        "OWCOPILOT_PROJECTS_JSON or send inline content"
                    ),
                )
            builder = project_context.context_builder
            bundle = project_context.bundle
        else:
            store, builder = _context_builder_for_bundle(req.content)
            bundle = req.content

        telemetry = TelemetryCollector()
        gateway = LLMGateway(
            providers={"cheap": OfflineQAProvider()},
            router=StaticRouter(mapping={"qa_answer": "cheap"}),
            cache=service_cache,  # app-lifetime L1/L2: repeated lore questions cost $0
            telemetry=telemetry,
        )
        try:
            answer = LoreQAService(
                gateway=gateway,
                context_builder=builder,
                bundle=bundle,
            ).ask(req.query, budget_tokens=req.budget_tokens)
            telemetry_summary = telemetry.summary()
            cost_budget = summarize_workflow(
                [llm_step("ask_lore", telemetry_summary)],
                budget_usd=req.max_cost_usd,
            ).budget
            return ProjectAskResponse(
                request_id=request_id,
                project=project,
                answer=answer.model_dump(mode="json"),
                telemetry=telemetry_summary,
                cost_budget=cost_budget.model_dump(mode="json"),
            )
        finally:
            if project_context is not None:
                project_context.close()
            if store is not None:
                store.close()

    def _registered_project(project: str) -> ProjectContext:
        project_context = _open_project_context(project)
        if project_context is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"project {project!r} is not registered; configure OWCOPILOT_PROJECTS_JSON"
                ),
            )
        return project_context

    def _task_gateway(
        req: _LLMModeRequest, *, task: str, offline_provider: Any
    ) -> tuple[LLMGateway | None, TelemetryCollector]:
        """Per-request gateway for v2 assist tasks. `offline_provider=None` with offline mode
        means the caller runs deterministically without any gateway (suggest).

        Real mode is fail-closed: it spends money, so it refuses to run unless the service is
        key-gated AND a provider is configured. The shared app-lifetime cache backs every
        request, so repeated questions/briefs hit L1/L2 instead of the provider."""
        telemetry = TelemetryCollector()
        if req.llm_mode == "real":
            if not os.getenv("OWCOPILOT_API_KEY"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "llm_mode=real over the API requires OWCOPILOT_API_KEY to be "
                        "configured (fail-closed: real mode spends provider credit)"
                    ),
                )
            if not os.getenv("OPENAI_API_KEY"):
                raise HTTPException(
                    status_code=503,
                    detail="real provider is not configured (OPENAI_BASE_URL / OPENAI_API_KEY)",
                )
            model: str = req.llm_model or os.getenv("OWCOPILOT_CHEAP_MODEL") or "deepseek-v4-flash"
            provider: Any = OpenAICompatProvider(model=model)
        elif offline_provider is None:
            return None, telemetry
        else:
            provider = offline_provider
        gateway = LLMGateway(
            providers={"cheap": provider},
            router=StaticRouter(mapping={task: "cheap"}),
            cache=service_cache,
            telemetry=telemetry,
            max_retries=1 if req.llm_mode == "real" else 0,
            retry_backoff_seconds=1.0 if req.llm_mode == "real" else 0.0,
        )
        return gateway, telemetry

    @app.post("/projects/{project}/impact:analyze", response_model=ProjectImpactResponse)
    def analyze_project_impact(
        project: str,
        req: ProjectImpactRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectImpactResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            changes: list[Change] = []
            for spec in req.changes:
                try:
                    change_type = ChangeType(spec.change_type)
                except ValueError as e:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"unknown change type {spec.change_type!r}; expected one of: "
                            + ", ".join(item.value for item in ChangeType)
                        ),
                    ) from e
                changes.append(Change(change_type=change_type, target_ref=spec.target_ref))
            result = ImpactAnalyzer(project_context.graph).analyze(
                ChangeSet(changes=changes), max_depth=req.max_depth
            )
            return ProjectImpactResponse(
                request_id=request_id,
                project=project,
                must_change=[
                    item.model_dump(mode="json")
                    for item in result.by_level(ImpactLevel.MUST_CHANGE)
                ],
                suggest_check=[
                    item.model_dump(mode="json")
                    for item in result.by_level(ImpactLevel.SUGGEST_CHECK)
                ],
                total=len(result.items),
                cost_budget=_deterministic_cost_budget("impact_of"),
            )
        finally:
            project_context.close()

    @app.post(
        "/projects/{project}/issues/{issue_id}/suggestions",
        response_model=ProjectSuggestResponse,
    )
    def create_issue_suggestions(
        project: str,
        issue_id: str,
        req: ProjectSuggestRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectSuggestResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            try:
                issue = find_issue(project_context, issue_id)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            gateway, telemetry = _task_gateway(req, task="patch_suggest", offline_provider=None)
            result = suggest_for_issue(
                project_context,
                issue,
                gateway=gateway,
                max_candidates=req.max_candidates,
                budget_tokens=req.budget_tokens,
            )
            telemetry_summary = telemetry.summary()
            cost_budget = (
                summarize_workflow(
                    [llm_step("patch_suggest", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget
                if result.used_llm
                else summarize_workflow([deterministic_step("patch_suggest")]).budget
            )
            return ProjectSuggestResponse(
                request_id=request_id,
                project=project,
                issue_id=issue_id,
                candidates=[
                    {
                        "patch_id": ranked.candidate.id,
                        "source": ranked.source,
                        "target_resolved": ranked.target_resolved,
                        "resolved_error_count": len(ranked.resolved_errors),
                        "ops": [op.model_dump(mode="json") for op in ranked.candidate.ops],
                        "rationale": ranked.candidate.rationale,
                    }
                    for ranked in result.candidates
                ],
                rejected_count=result.rejected_count,
                parse_failed=result.parse_failed,
                used_llm=result.used_llm,
                telemetry=telemetry_summary,
                cost_budget=cost_budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    @app.post(
        "/projects/{project}/patches/{patch_id}:apply",
        response_model=ProjectPatchApplyResponse,
    )
    def apply_project_patch(
        project: str,
        patch_id: str,
        req: ProjectPatchDecisionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectPatchApplyResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            try:
                outcome = apply_patch_workflow(project_context, patch_id, operator=req.operator)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            return ProjectPatchApplyResponse(
                request_id=request_id,
                project=project,
                applied=outcome.applied,
                patch_id=outcome.patch_id,
                reason=outcome.reason,
                introduced_errors=outcome.introduced_errors,
                resolved_errors=outcome.resolved_errors,
                post_audit_open_errors=outcome.post_audit_open_errors,
                cost_budget=_deterministic_cost_budget("apply_patch"),
            )
        finally:
            project_context.close()

    @app.post(
        "/projects/{project}/patches/{patch_id}:rollback",
        response_model=ProjectPatchRollbackResponse,
    )
    def rollback_project_patch(
        project: str,
        patch_id: str,
        req: ProjectPatchDecisionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectPatchRollbackResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            try:
                outcome = rollback_patch_workflow(project_context, patch_id, operator=req.operator)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            except ValueError as e:
                raise HTTPException(status_code=409, detail=str(e)) from e
            return ProjectPatchRollbackResponse(
                request_id=request_id,
                project=project,
                rolled_back=outcome.rolled_back,
                patch_id=outcome.patch_id,
                post_audit_open_errors=outcome.post_audit_open_errors,
                cost_budget=_deterministic_cost_budget("rollback_patch"),
            )
        finally:
            project_context.close()

    @app.post("/projects/{project}/contents/quests:draft", response_model=ProjectDraftResponse)
    def draft_project_quest(
        project: str,
        req: ProjectDraftRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectDraftResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req, task="quest_draft", offline_provider=OfflineQuestDraftProvider()
            )
            assert gateway is not None
            result = QuestDraftService(
                gateway=gateway,
                context_builder=project_context.context_builder,
                audit_runner=project_context.audit_runner,
                bundle=project_context.bundle,
            ).draft_quest(req.brief, budget_tokens=req.budget_tokens)
            queue = ReviewQueue(project_context.sqlite_store)
            item = queue.add_quest_draft(
                result.quest.model_dump(mode="json", exclude_none=True),
                issue_refs=[issue_fingerprint(issue) for issue in result.issues],
            )
            telemetry_summary = telemetry.summary()
            return ProjectDraftResponse(
                request_id=request_id,
                project=project,
                quest=result.quest.model_dump(mode="json", exclude_none=True),
                issues=[issue.model_dump(mode="json") for issue in result.issues],
                review_item_id=item.id,
                telemetry=telemetry_summary,
                cost_budget=summarize_workflow(
                    [llm_step("quest_draft", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    @app.post("/projects/{project}/assist/barks:batch", response_model=ProjectBarksResponse)
    def batch_project_barks(
        project: str,
        req: ProjectBarksRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectBarksResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            unknown = [
                speaker
                for speaker in req.speaker_ids
                if speaker not in project_context.bundle.entities
            ]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown speaker entities: {', '.join(unknown)}",
                )
            gateway, telemetry = _task_gateway(
                req, task="barks_batch", offline_provider=OfflineBarksProvider()
            )
            assert gateway is not None
            allowed = set(req.speaker_ids) | set(req.allowed_entity_ids)
            result = BarkBatchService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
            ).generate(
                speaker_ids=req.speaker_ids,
                topic=req.topic,
                variants_per_speaker=req.variants_per_speaker,
                max_chars=req.max_chars,
                allowed_entity_ids=allowed,
            )
            telemetry_summary = telemetry.summary()
            return ProjectBarksResponse(
                request_id=request_id,
                project=project,
                accepted=[
                    {"speaker_id": variant.speaker_id, "text": variant.text}
                    for variant in result.accepted
                ],
                rejected=[
                    {
                        "speaker_id": rejected.speaker_id,
                        "text": rejected.text,
                        "issues": [issue.model_dump(mode="json") for issue in rejected.issues],
                    }
                    for rejected in result.rejected
                ],
                review_item_ids=[item.id for item in result.review_items],
                telemetry=telemetry_summary,
                cost_budget=summarize_workflow(
                    [llm_step("barks_batch", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    @app.post("/projects/{project}/extractions:run", response_model=ProjectExtractionRunResponse)
    def run_project_extraction(
        project: str,
        req: ProjectExtractionRunRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectExtractionRunResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req, task="extract_lore", offline_provider=OfflineExtractionProvider()
            )
            assert gateway is not None
            draft = ExtractionService(gateway=gateway, bundle=project_context.bundle).extract(
                title=req.title,
                text=req.text,
                source_kind=req.source_kind,
                max_chunks=req.max_chunks,
            )
            telemetry_summary = telemetry.summary()
            return ProjectExtractionRunResponse(
                request_id=request_id,
                project=project,
                draft=draft.model_dump(mode="json", exclude_none=True),
                stats=draft.stats,
                telemetry=telemetry_summary,
                cost_budget=summarize_workflow(
                    [llm_step("extract_lore", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    @app.post(
        "/projects/{project}/extractions:submit",
        response_model=ProjectExtractionSubmitResponse,
    )
    def submit_project_extraction(
        project: str,
        req: ProjectExtractionSubmitRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectExtractionSubmitResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            parsed = ExtractionDraft.model_validate(req.draft)
        except Exception as e:
            raise HTTPException(status_code=422, detail=f"invalid extraction draft: {e}") from e
        if req.answers:
            parsed = apply_gap_answers(parsed, req.answers)
        if req.include_beats_as_quests:
            parsed.bundle.quests.update(quests_from_beats(parsed))
        project_context = _registered_project(project)
        try:
            issues = project_context.audit_runner.run(
                AuditContext.from_bundle(parsed.bundle)
            ).issues
            item = ReviewQueue(project_context.sqlite_store).add_import_draft(
                {
                    "id": parsed.id,
                    "source_title": parsed.source_title,
                    "source_kind": parsed.source_kind,
                    "summary": parsed.summary,
                    "bundle": parsed.bundle.model_dump(mode="json", exclude_none=True),
                    "plot_beats": [b.model_dump(mode="json") for b in parsed.plot_beats],
                    "open_gaps": [g.model_dump(mode="json") for g in parsed.gaps],
                },
                issue_refs=[issue_fingerprint(issue) for issue in issues],
            )
            return ProjectExtractionSubmitResponse(
                request_id=request_id,
                project=project,
                review_item_id=item.id,
                open_gaps=len(parsed.gaps),
                issues=[issue.model_dump(mode="json") for issue in issues],
            )
        finally:
            project_context.close()

    @app.post(
        "/projects/{project}/assist/dialogue_trees:draft",
        response_model=ProjectDialogueTreeResponse,
    )
    def draft_project_dialogue_tree(
        project: str,
        req: ProjectDialogueTreeRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectDialogueTreeResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            unknown = [
                pid for pid in req.participant_ids if pid not in project_context.bundle.entities
            ]
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"unknown participant entities: {', '.join(unknown)}",
                )
            gateway, telemetry = _task_gateway(
                req, task="dialogue_tree", offline_provider=OfflineDialogueTreeProvider()
            )
            assert gateway is not None
            result = DialogueTreeService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
            ).generate(
                participant_ids=req.participant_ids,
                brief=req.brief,
                quest_id=req.quest_id,
                max_nodes=req.max_nodes,
                max_chars=req.max_chars,
            )
            telemetry_summary = telemetry.summary()
            return ProjectDialogueTreeResponse(
                request_id=request_id,
                project=project,
                tree=result.tree.model_dump(mode="json", exclude_none=True),
                lint_issues=[i.model_dump(mode="json") for i in result.lint_issues],
                structure_problems=result.structure_problems,
                review_item_id=result.review_item.id if result.review_item else None,
                telemetry=telemetry_summary,
                cost_budget=summarize_workflow(
                    [llm_step("dialogue_tree", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    @app.post("/projects/{project}/assist/flavor:batch", response_model=ProjectFlavorResponse)
    def batch_project_flavor(
        project: str,
        req: ProjectFlavorRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectFlavorResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req, task="flavor_batch", offline_provider=OfflineFlavorProvider()
            )
            assert gateway is not None
            result = FlavorBatchService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
            ).generate(
                category=req.category,
                names=req.names,
                theme=req.theme,
                max_chars=req.max_chars,
            )
            telemetry_summary = telemetry.summary()
            return ProjectFlavorResponse(
                request_id=request_id,
                project=project,
                batch_id=result.batch_id,
                accepted=[e.model_dump(mode="json") for e in result.accepted],
                rejected=[
                    {"name": r.name, "text": r.text, "issues": [i.code for i in r.issues]}
                    for r in result.rejected
                ],
                review_item_id=result.review_item.id if result.review_item else None,
                telemetry=telemetry_summary,
                cost_budget=summarize_workflow(
                    [llm_step("flavor_batch", telemetry_summary)],
                    budget_usd=req.max_cost_usd,
                ).budget.model_dump(mode="json"),
            )
        finally:
            project_context.close()

    # ---- round-13 platform endpoints: the standardized surface any client (the Vue
    # frontend, studio pipelines, CI) integrates against. Same gates as everything else:
    # optional API key, rate limit, real mode fail-closed.
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

    @app.get("/workspaces", response_model=WorkspaceListResponse)
    def list_workspaces(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> WorkspaceListResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        return WorkspaceListResponse(workspaces=[WorkspaceInfo(**w) for w in list_managed_worlds()])

    @app.post("/workspaces", response_model=WorkspaceInfo, status_code=201)
    def create_workspace(
        req: WorkspaceCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> WorkspaceInfo:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            created = create_managed_world(req.name)
        except ValueError as e:
            raise HTTPException(status_code=409, detail=str(e)) from e
        return WorkspaceInfo(name=created.name, path=str(created))

    @app.post("/workspaces:import", response_model=WorkspaceInfo, status_code=201)
    def import_workspace(
        req: WorkspaceImportRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> WorkspaceInfo:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            data = base64.b64decode(req.zip_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(status_code=400, detail="zip_base64 不是有效的 base64") from e
        try:
            imported = import_world_zip(data, req.name)
        except ValueError as e:
            # name collision is a conflict; anything else (slip, empty, not a world
            # pack) is a bad request
            status = 409 if "已存在" in str(e) else 400
            raise HTTPException(status_code=status, detail=str(e)) from e
        return WorkspaceInfo(name=imported.name, path=str(imported))

    @app.get("/workspaces/{name}/pack")
    def download_workspace_pack(
        name: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Response:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            safe_name = sanitize_world_name(name)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        target = worlds_home() / safe_name
        if not target.exists():
            raise HTTPException(status_code=404, detail=f"世界「{safe_name}」不存在")
        # HTTP headers are latin-1: CJK names need the RFC 5987 filename* form, with a
        # plain ASCII fallback for ancient clients
        encoded = urllib.parse.quote(f"{safe_name}-pack.zip")
        return Response(
            content=export_world_zip(target),
            media_type="application/zip",
            headers={
                "Content-Disposition": (
                    f"attachment; filename=\"world-pack.zip\"; filename*=UTF-8''{encoded}"
                )
            },
        )

    @app.get("/projects/{project}/overview")
    def project_overview(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        return {
            "project": project,
            "overview": build_project_overview(_project_root_or_404(project)),
        }

    @app.get("/projects/{project}/archive")
    def project_archive(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        return {
            "project": project,
            "inventory": build_content_inventory(_project_root_or_404(project)),
        }

    @app.post("/projects/{project}/jobs", response_model=JobCreatedResponse, status_code=202)
    def create_job(
        project: str,
        req: JobCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JobCreatedResponse:
        """Run a long action asynchronously; progress streams over /jobs/{id}/events."""
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        allowed = _JOB_PARAM_KEYS[req.kind]
        unknown = sorted(set(req.params) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown params for {req.kind}: {unknown}; allowed: {sorted(allowed)}",
            )
        if req.params.get("llm_mode") == "real":
            if not os.getenv("OWCOPILOT_API_KEY"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "llm_mode=real over the API requires OWCOPILOT_API_KEY to be "
                        "configured (fail-closed: real mode spends provider credit)"
                    ),
                )
            if not os.getenv("OPENAI_API_KEY"):
                raise HTTPException(
                    status_code=503,
                    detail="real provider is not configured (OPENAI_BASE_URL / OPENAI_API_KEY)",
                )
        content_root = _project_root_or_404(project)
        runners: dict[str, Callable[..., dict[str, Any]]] = {
            "world_seed": run_world_seed_action,
            "extraction": run_extraction_action,
            "theme_sweep": run_theme_sweep_action,
        }
        action = runners[req.kind]
        params = dict(req.params)

        def runner(emit: Callable[[str, dict[str, Any]], None]) -> dict[str, Any]:
            return action(content_root, progress=emit, **params)

        job = jobs_manager.submit(req.kind, runner)
        return JobCreatedResponse(job_id=job.id, kind=job.kind, status=job.status)

    @app.get("/jobs/{job_id}", response_model=JobStatusResponse)
    def job_status(
        job_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JobStatusResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        job = jobs_manager.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
        return JobStatusResponse(
            job_id=job.id,
            kind=job.kind,
            status=job.status,
            events=list(job.events),
            result=job.result,
            error=job.error,
        )

    @app.get("/jobs/{job_id}/events")
    def job_events(
        job_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> StreamingResponse:
        """SSE: replays buffered events, then tails until the job is terminal."""
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        if jobs_manager.get(job_id) is None:
            raise HTTPException(status_code=404, detail=f"job not found: {job_id}")

        def stream():
            index = 0
            while True:
                events, index, terminal = jobs_manager.wait_events(job_id, index, timeout=10.0)
                for event in events:
                    payload = json.dumps(event["data"], ensure_ascii=False)
                    yield f"event: {event['type']}\ndata: {payload}\n\n"
                if terminal and not events:
                    return
                if not events and not terminal:
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/projects/{project}/review_items", response_model=ReviewItemsResponse)
    def list_review_items(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ReviewItemsResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        result = list_review_items_action(_project_root_or_404(project))
        return ReviewItemsResponse(project=project, **result)

    @app.post(
        "/projects/{project}/review_items/{item_id}:decide",
        response_model=ReviewDecideResponse,
    )
    def decide_review_item_endpoint(
        project: str,
        item_id: str,
        req: ReviewDecideRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ReviewDecideResponse:
        """The HITL write path over REST: decisions are final (the backend guard turns a
        double decide into 409, so client retries cannot corrupt provenance)."""
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            decided = decide_review_action(
                _project_root_or_404(project),
                item_id=item_id,
                decision=req.decision,
                operator=req.operator,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"review item not found: {e}") from e
        except ValueError as e:
            raise _manage_error(e) from e
        return ReviewDecideResponse(
            project=project,
            item_id=item_id,
            decision=str(decided.get("decision", req.decision)),
            written_ref=decided.get("written_ref"),
            post_audit_open_errors=int(decided.get("post_audit_open_errors", 0)),
            cost_budget=decided.get("cost_budget") or {},
        )

    @app.post("/projects/{project}/sweeps:run", response_model=ProjectSweepResponse)
    def run_theme_sweep(
        project: str,
        req: ProjectSweepRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectSweepResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        if req.use_llm and req.llm_mode == "real":
            if not os.getenv("OWCOPILOT_API_KEY"):
                raise HTTPException(
                    status_code=403,
                    detail=(
                        "llm_mode=real over the API requires OWCOPILOT_API_KEY to be "
                        "configured (fail-closed: real mode spends provider credit)"
                    ),
                )
            if not os.getenv("OPENAI_API_KEY"):
                raise HTTPException(
                    status_code=503,
                    detail="real provider is not configured (OPENAI_BASE_URL / OPENAI_API_KEY)",
                )
        result = run_theme_sweep_action(
            _project_root_or_404(project),
            theme=req.theme,
            extra_terms=req.extra_terms,
            use_llm=req.use_llm,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model,
            max_judge=req.max_judge,
        )
        return ProjectSweepResponse(
            project=project, **{k: v for k, v in result.items() if k != "terms"}
        )

    @app.patch("/projects/{project}/entities/{entity_id}", response_model=EntityUpdateResponse)
    def update_entity(
        project: str,
        entity_id: str,
        req: EntityUpdateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> EntityUpdateResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            result = update_entity_action(
                _project_root_or_404(project),
                entity_id=entity_id,
                name=req.name,
                description=req.description,
                tags=req.tags,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return EntityUpdateResponse(project=project, **result)

    @app.delete(
        "/projects/{project}/objects/{ref_type}/{object_id}",
        response_model=ObjectDeleteResponse,
    )
    def delete_object(
        project: str,
        ref_type: str,
        object_id: str,
        request: Request,
        cascade_relations: bool = True,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ObjectDeleteResponse:
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
        try:
            result = delete_object_action(
                _project_root_or_404(project),
                ref_type=ref_type,
                object_id=object_id,
                cascade_relations=cascade_relations,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return ObjectDeleteResponse(project=project, **result)

    @app.post("/projects/{project}/exports", response_model=ProjectExportResponse)
    def export_project_content(
        project: str,
        req: ProjectExportRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectExportResponse:
        request_id = str(uuid.uuid4())
        _require_api_key(x_api_key)
        limiter.check(_client_key(x_api_key, request))
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
            manifest = export_content_bundle(
                project_context.bundle, output_dir, target_engine=engine
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

    return app


app = create_app()
