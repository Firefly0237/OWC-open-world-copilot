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

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..app.actions import (
    add_reference_action,
    add_relation_action,
    apply_rename_action,
    asset_attach_action,
    asset_detach_action,
    asset_list_action,
    assign_action,
    collab_state_action,
    comment_action,
    compliance_report_action,
    create_entity_action,
    decide_review_action,
    delete_mapping_template_action,
    delete_object_action,
    detect_contradictions_action,
    draft_quest_logic_action,
    import_from_engine_action,
    instantiate_template_action,
    list_mapping_templates_action,
    list_references_action,
    list_review_items_action,
    list_templates_action,
    loc_assign_action,
    loc_transition_action,
    localization_overview_action,
    lock_action,
    plan_rename_action,
    probe_llm_connection_action,
    recognize_apply_plan_action,
    recognize_content_action,
    remove_relation_action,
    rescan_case_action,
    restore_snapshot_action,
    reviewer_calibration_action,
    revise_draft_action,
    run_build_overview_action,
    run_character_action,
    run_compliance_scan_action,
    run_extraction_action,
    run_ingest_action,
    run_theme_sweep_action,
    run_world_expand_action,
    run_world_seed_action,
    save_mapping_template_action,
    search_all_action,
    search_references_action,
    set_object_position_action,
    simulate_quest_action,
    transition_case_action,
    update_dialogue_tree_action,
    update_entity_action,
    update_quest_action,
    update_quest_logic_action,
    update_style_guide_action,
    world_analytics_action,
)
from ..app.view_models import (
    build_content_inventory,
    build_dialogue_flow_view_model,
    build_dialogue_list_view_model,
    build_dialogue_tree_view_model,
    build_diff_view_model,
    build_graph_view_model,
    build_project_overview,
    build_quest_view_model,
    build_readiness_report,
    build_snapshots_view_model,
    build_timeline_view_model,
    create_world_snapshot,
    relation_kinds_view_model,
)
from ..app.workspaces import (
    create_managed_world,
    delete_managed_world,
    export_world_zip,
    import_world_zip,
    list_managed_worlds,
    sanitize_world_name,
    worlds_home,
)
from ..assembly import PrefixMode, RouterMode, build_grounded_pipeline
from ..assist.barks import BarkBatchService
from ..assist.critic import BarkCritic, DialogueCritic, FlavorCritic, QuestCritic
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
from ..exporters.lorebook import write_lorebook
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
from ..llm.gateway import (
    OFFLINE_LLM_FORBIDDEN_MESSAGE,
    LLMGateway,
    LLMGatewayError,
    OpenAICompatProvider,
    StructuredFakeProvider,
    offline_llm_allowed,
)
from ..llm.router import StaticRouter
from ..llm.telemetry import TelemetryCollector, prices_are_configured
from ..pipeline.audit import run_full_audit
from ..pipeline.patches import (
    apply_patch_workflow,
    find_issue,
    rollback_patch_workflow,
    suggest_for_issue,
)
from ..pipeline.project import ProjectContext
from ..platform import (
    AuditEntry,
    AuthError,
    Membership,
    PlatformStore,
    Principal,
    Role,
    Tenant,
    User,
    mint_token,
    require_role,
    resolve_principal,
)
from ..qa.offline import OfflineQAProvider
from ..qa.service import LoreQAService
from ..retrieval.bm25 import BM25Retriever
from ..retrieval.context_pack import ContextPackBuilder
from ..retrieval.embedding import resolve_embedder
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
    """`real` (OpenAI-compatible provider) or `offline` (deterministic doubles).

    `offline` keeps startup resilient with no provider configured — deterministic features
    (audit / impact / export / retrieval) work and AI features fail closed — but the doubles
    themselves only run when `OWCOPILOT_ALLOW_OFFLINE_LLM` is set (a test/CI fixture, never a
    shipped product mode). Read per request so the mode can be flipped by env without restarting.
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
        embedder=resolve_embedder(),
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
    llm_mode: str = Field(default="offline", pattern="^(offline|real)$")
    llm_model: str = Field(default="deepseek-v4-flash", max_length=120)


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
    # Accept a whole novel. Coverage is planned server-side (see extraction.plan_coverage): the
    # whole document is read within a bounded call budget, so a large upload is never rejected at
    # the boundary nor silently truncated — at worst it is covered partially and reported as such.
    text: str = Field(min_length=1, max_length=2_000_000)
    source_kind: str = Field(default="文稿", max_length=40)


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
    refine_rounds: int = Field(default=1, ge=0, le=4)


class ProjectDialogueTreeResponse(BaseModel):
    request_id: str
    project: str
    tree: dict[str, Any]
    lint_issues: list[dict[str, Any]]
    structure_problems: list[str]
    refine_trail: list[dict[str, Any]] = Field(default_factory=list)
    auto_review_incomplete: bool = False
    review_item_id: str | None
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectFlavorRequest(_LLMModeRequest):
    category: str = Field(pattern="^(item|skill|achievement)$")
    names: list[str] = Field(min_length=1, max_length=50)
    theme: str = Field(default="", max_length=200)
    max_chars: int = Field(default=120, ge=20, le=400)
    refine_rounds: int = Field(default=0, ge=0, le=4)


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
    refine_rounds: int = Field(default=2, ge=0, le=4)


class ProjectDraftResponse(BaseModel):
    request_id: str
    project: str
    quest: dict[str, Any]
    issues: list[dict[str, Any]]
    refine_trail: list[dict[str, Any]] = Field(default_factory=list)
    auto_review_incomplete: bool = False
    review_item_id: str
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectBarksRequest(_LLMModeRequest):
    speaker_ids: list[str] = Field(min_length=1, max_length=50)
    topic: str = Field(min_length=1, max_length=1000)
    variants_per_speaker: int = Field(default=4, ge=1, le=10)
    max_chars: int = Field(default=40, ge=8, le=500)
    allowed_entity_ids: list[str] = Field(default_factory=list)
    refine_rounds: int = Field(default=0, ge=0, le=4)


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


class SnapshotCreateRequest(BaseModel):
    label: str = Field(default="", max_length=120)


class EntityCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    type: str = Field(min_length=1)
    description: str = Field(default="", max_length=4000)
    tags: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class QuestPatchRequest(BaseModel):
    title: str | None = None
    objective: str | None = None
    timeline_order: int | None = None
    set_timeline_order: bool = False  # distinguish "clear order" (None) from "leave unchanged"
    prerequisites: list[str] | None = None
    giver_npc: str | None = None
    location: str | None = None
    if_match: str | None = None  # WS-B optimistic-concurrency etag


class AssignRequest(BaseModel):
    object_ref: str = Field(min_length=1)
    assignee: str = ""  # empty clears the assignment
    by: str = Field(min_length=1)
    note: str = ""


class CommentRequest(BaseModel):
    object_ref: str = Field(min_length=1)
    author: str = Field(min_length=1)
    body: str = Field(min_length=1)


class LockRequest(BaseModel):
    object_ref: str = Field(min_length=1)
    holder: str = Field(min_length=1)
    release: bool = False


class TemplateInstantiateRequest(BaseModel):
    template_id: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)


class LocTransitionRequest(BaseModel):
    text_key: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    to: str = Field(min_length=1)
    by: str = Field(min_length=1)


class LocAssignRequest(BaseModel):
    text_key: str = Field(min_length=1)
    locale: str = Field(min_length=1)
    assignee: str = ""


class QuestLogicDraftRequest(_LLMModeRequest):
    intent: str = ""
    refine_rounds: int = Field(default=2, ge=0, le=4)


class QuestLogicPatchRequest(BaseModel):
    logic: dict[str, Any] | None = None  # the QuestLogic payload; null clears the logic layer


class QuestSimulateRequest(BaseModel):
    choices: list[str] | None = None  # branch ids to take at branching stages
    initial_state: dict[str, Any] | None = None  # seed variable / quest-state values


class RenameRequest(BaseModel):
    ref: str = Field(min_length=1)
    new_name: str | None = None
    new_id: str | None = None
    operator: str = ""  # required for :apply


class SnapshotRestoreRequest(BaseModel):
    snapshot_id: str = Field(min_length=1)


class ComplianceScanRequest(_LLMModeRequest):
    rule_pack: dict[str, Any] | None = None


class CaseTransitionRequest(BaseModel):
    to: str = Field(min_length=1)
    operator: str = Field(min_length=1)
    note: str = ""
    assignee: str | None = None


class CaseRescanRequest(_LLMModeRequest):
    operator: str = Field(min_length=1)
    rule_pack: dict[str, Any] | None = None


class AssetAttachRequest(BaseModel):
    object_ref: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    uri: str = Field(min_length=1)
    title: str = ""


class EngineImportRequest(BaseModel):
    quests: list[dict[str, Any]] = Field(min_length=1)


class RecognizeRequest(BaseModel):
    """Recognize a foreign project file's content into an editable plan. ``content`` is pasted text
    or ``content_base64`` is uploaded raw bytes (required for binary .xlsx / non-UTF-8 CSV).
    ``source_format='auto'`` sniffs the format. The server reads no local path."""

    source_format: str = Field(pattern="^(auto|table|articy|ink|yarn|ue|unity)$")
    content: str | None = None
    content_base64: str | None = None
    filename: str = "upload"
    field_mapping: dict[str, Any] | None = None
    apply: bool = False
    enable_llm: bool = False
    operator: str = "import"


class RecognizeApplyRequest(BaseModel):
    """Stage an edited ImportPlan (the human kept/dropped proposals in the UI) into review."""

    plan: dict[str, Any]
    operator: str = "import"


class MappingTemplateRequest(BaseModel):
    name: str = Field(min_length=1)
    mapping: dict[str, Any]


class TenantCreateRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    owner_email: str = Field(min_length=3)


class MembershipRequest(BaseModel):
    tenant_id: str = Field(min_length=1)
    user_id: str = Field(min_length=1)
    email: str = Field(min_length=3)
    role: str = "editor"


class DevTokenRequest(BaseModel):
    user_id: str = Field(min_length=1)
    tenant_id: str = Field(min_length=1)
    role: str = "editor"
    ttl_seconds: int = Field(default=3600, ge=60, le=86400)


class RelationRequest(BaseModel):
    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: str = Field(min_length=1, max_length=60)
    symmetric: bool | None = None


class DialogueTreePatchRequest(BaseModel):
    title: str | None = None
    root_node: str | None = None
    nodes: dict[str, Any] | None = None
    metadata_updates: dict[str, Any] | None = None


class NodePositionRequest(BaseModel):
    ref: str = Field(min_length=1)
    x: float
    y: float


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


class ReviewReviseRequest(BaseModel):
    feedback: str = Field(min_length=1, max_length=2000)
    operator: str = Field(min_length=1, max_length=80)
    budget_tokens: int = Field(default=800, ge=1, le=8000)
    llm_mode: str = Field(default="offline", pattern="^(offline|real)$")
    llm_model: str = Field(default="deepseek-v4-flash", max_length=120)


class ReviewReviseResponse(BaseModel):
    project: str
    item_id: str
    item: dict[str, Any]
    revised_payload: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectSweepRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=200)
    extra_terms: list[str] = Field(default_factory=list)
    use_llm: bool = False
    llm_mode: str = "offline"
    llm_model: str = "deepseek-v4-flash"
    max_judge: int = Field(default=400, ge=1, le=2000)


class StyleGuidePatchRequest(BaseModel):
    body: str | None = None
    rules: list[str] | None = None


class ContradictionScanRequest(_LLMModeRequest):
    use_llm: bool = False
    semantic_threshold: float = Field(default=0.6, ge=0.0, le=1.0)
    max_judge: int = Field(default=200, ge=1, le=1000)


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


class ReferenceAddRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    # A whole reference book is fine: the inspiration store chunks + BM25-indexes it, and genesis
    # retrieves only the budget-bounded relevant chunks — so length never overruns a model call.
    text: str = Field(min_length=1, max_length=2_000_000)
    source_type: str = Field(default="uploaded_file", max_length=40)
    original_filename: str | None = Field(default=None, max_length=200)


class ReferenceSearchRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2000)
    budget_tokens: int = Field(default=1000, ge=1, le=8000)


class IngestRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)
    dry_run: bool = True
    write_non_conflicting: bool = False


_JOB_KINDS = (
    "world_seed",
    "world_expand",
    "extraction",
    "theme_sweep",
    "character_profile",
    "build_overview",
)
_JOB_PARAM_KEYS: dict[str, set[str]] = {
    "world_seed": {"brief", "llm_mode", "llm_model", "budget_tokens", "refine_rounds"},
    "world_expand": {"brief", "llm_mode", "llm_model", "budget_tokens", "refine_rounds"},
    "extraction": {
        "title",
        "text",
        "source_kind",
        "verify_faithfulness",
        "llm_mode",
        "llm_model",
    },
    "theme_sweep": {
        "theme",
        "extra_terms",
        "use_llm",
        "llm_mode",
        "llm_model",
        "max_judge",
        "semantic_threshold",
    },
    "character_profile": {"brief", "llm_mode", "llm_model", "budget_tokens", "refine_rounds"},
    "build_overview": {"llm_mode", "llm_model"},
}


class ConnectionStatusResponse(BaseModel):
    configured: bool
    base_url: str = ""
    # whether real per-tier prices are set (OWCOPILOT_PRICE_*); when false the reported cost is a
    # ballpark estimate from illustrative prices, which the UI labels accordingly.
    prices_configured: bool = False


class ConnectionUpdateRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""


class ConnectionProbeRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = Field(min_length=1, max_length=120)


_JOB_KIND_PATTERN = "^(" + "|".join(_JOB_KINDS) + ")$"


class JobCreateRequest(BaseModel):
    kind: str = Field(pattern=_JOB_KIND_PATTERN)
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
    metadata_updates: dict[str, Any] | None = None


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
    jobs_manager = JobManager()
    app.state.v2_issues = {}
    # WS-P control plane: SQLite by default (dev/CI), Postgres in production (see deploy/). Holds
    # only tenancy metadata + audit; the canon stays file-backed.
    platform_store = PlatformStore(os.getenv("OWCOPILOT_PLATFORM_DB", ":memory:"))

    def _principal(authorization: str | None, x_api_key: str | None) -> Principal:
        try:
            return resolve_principal(
                authorization=authorization,
                x_api_key=x_api_key,
                expected_api_key=os.getenv("OWCOPILOT_API_KEY"),
            )
        except AuthError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

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

    @app.post("/quests:generate", response_model=GenerateResponse)
    def generate_quest(
        req: GenerateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> GenerateResponse:
        request_id = str(uuid.uuid4())
        if llm_mode != "real":
            _require_offline_allowed()

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
        if llm_mode != "real":
            _require_offline_allowed()

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
        if req.llm_mode == "real":
            _require_real_allowed(request)
            ask_provider: Any = OpenAICompatProvider(model=req.llm_model, timeout=60.0)
        else:
            _require_offline_allowed()
            ask_provider = OfflineQAProvider()
        gateway = LLMGateway(
            providers={"cheap": ask_provider},
            router=StaticRouter(mapping={"qa_answer": "cheap"}),
            cache=service_cache,  # app-lifetime L1/L2: repeated lore questions cost $0
            telemetry=telemetry,
            max_retries=1 if req.llm_mode == "real" else 0,
            retry_backoff_seconds=1.0 if req.llm_mode == "real" else 0.0,
            namespace=project,  # scope cached answers to this project (shared app-lifetime cache)
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
        req: _LLMModeRequest,
        *,
        request: Request,
        task: str,
        offline_provider: Any,
        namespace: str = "",
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
            model: str = req.llm_model or os.getenv("OWCOPILOT_CHEAP_MODEL") or "deepseek-v4-flash"
            provider: Any = OpenAICompatProvider(model=model)
        elif offline_provider is None:
            return None, telemetry
        else:
            _require_offline_allowed()
            provider = offline_provider
        gateway = LLMGateway(
            providers={"cheap": provider},
            router=StaticRouter(mapping={task: "cheap"}),
            cache=service_cache,
            telemetry=telemetry,
            max_retries=1 if req.llm_mode == "real" else 0,
            retry_backoff_seconds=1.0 if req.llm_mode == "real" else 0.0,
            namespace=namespace,
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
        project_context = _registered_project(project)
        try:
            try:
                issue = find_issue(project_context, issue_id)
            except FileNotFoundError as e:
                raise HTTPException(status_code=404, detail=str(e)) from e
            gateway, telemetry = _task_gateway(
                req, request=request, task="patch_suggest", offline_provider=None, namespace=project
            )
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
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req,
                request=request,
                task="quest_draft",
                offline_provider=OfflineQuestDraftProvider(),
                namespace=project,
            )
            assert gateway is not None
            critic = QuestCritic(gateway=gateway) if req.refine_rounds > 0 else None
            result = QuestDraftService(
                gateway=gateway,
                context_builder=project_context.context_builder,
                audit_runner=project_context.audit_runner,
                bundle=project_context.bundle,
                critic=critic,
                max_refine_rounds=req.refine_rounds,
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
                refine_trail=[r.model_dump(mode="json") for r in result.refine_trail],
                auto_review_incomplete=result.auto_review_incomplete,
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
                req,
                request=request,
                task="barks_batch",
                offline_provider=OfflineBarksProvider(),
                namespace=project,
            )
            assert gateway is not None
            allowed = set(req.speaker_ids) | set(req.allowed_entity_ids)
            result = BarkBatchService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
                critic=BarkCritic(gateway=gateway) if req.refine_rounds > 0 else None,
                max_refine_rounds=req.refine_rounds,
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
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req,
                request=request,
                task="extract_lore",
                offline_provider=OfflineExtractionProvider(),
                namespace=project,
            )
            assert gateway is not None
            draft = ExtractionService(gateway=gateway, bundle=project_context.bundle).extract(
                title=req.title,
                text=req.text,
                source_kind=req.source_kind,
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
                req,
                request=request,
                task="dialogue_tree",
                offline_provider=OfflineDialogueTreeProvider(),
                namespace=project,
            )
            assert gateway is not None
            critic = DialogueCritic(gateway=gateway) if req.refine_rounds > 0 else None
            result = DialogueTreeService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
                critic=critic,
                max_refine_rounds=req.refine_rounds,
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
                refine_trail=[r.model_dump(mode="json") for r in result.refine_trail],
                auto_review_incomplete=result.auto_review_incomplete,
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
        project_context = _registered_project(project)
        try:
            gateway, telemetry = _task_gateway(
                req,
                request=request,
                task="flavor_batch",
                offline_provider=OfflineFlavorProvider(),
                namespace=project,
            )
            assert gateway is not None
            result = FlavorBatchService(
                gateway=gateway,
                bundle=project_context.bundle,
                review_queue=ReviewQueue(project_context.sqlite_store),
                critic=FlavorCritic(gateway=gateway) if req.refine_rounds > 0 else None,
                max_refine_rounds=req.refine_rounds,
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

    @app.get("/settings/connection", response_model=ConnectionStatusResponse)
    def connection_status(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ConnectionStatusResponse:
        # status must mirror what a real call would see: the gateway dotenv-loads on
        # every real run, so do the same here or a repo .env reads as "not connected"
        load_dotenv()
        return ConnectionStatusResponse(
            configured=bool(os.getenv("OPENAI_API_KEY", "").strip()),
            base_url=os.getenv("OPENAI_BASE_URL", ""),
            prices_configured=prices_are_configured(),
        )

    @app.post("/settings/connection", response_model=ConnectionStatusResponse)
    def update_connection(
        req: ConnectionUpdateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ConnectionStatusResponse:
        """Wire the provider connection into the server process (same mechanism the
        legacy UI used). Local-machine semantics: only loopback may change it — a remote
        client must never reconfigure whose key this server spends."""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="connection settings are local-only")
        if req.base_url.strip():
            os.environ["OPENAI_BASE_URL"] = req.base_url.strip()
        if req.api_key.strip():
            os.environ["OPENAI_API_KEY"] = req.api_key.strip()
        return ConnectionStatusResponse(
            configured=bool(os.getenv("OPENAI_API_KEY", "").strip()),
            base_url=os.getenv("OPENAI_BASE_URL", ""),
        )

    @app.post("/settings/connection:probe")
    def probe_connection(
        req: ConnectionProbeRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        if not _is_loopback(request) and not os.getenv("OWCOPILOT_API_KEY"):
            raise HTTPException(status_code=403, detail="probe is local-only on open servers")
        return probe_llm_connection_action(
            base_url=req.base_url or os.getenv("OPENAI_BASE_URL", ""),
            api_key=req.api_key or os.getenv("OPENAI_API_KEY", ""),
            model=req.model,
        )

    @app.get("/workspaces", response_model=WorkspaceListResponse)
    def list_workspaces(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> WorkspaceListResponse:
        return WorkspaceListResponse(workspaces=[WorkspaceInfo(**w) for w in list_managed_worlds()])

    @app.post("/workspaces", response_model=WorkspaceInfo, status_code=201)
    def create_workspace(
        req: WorkspaceCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> WorkspaceInfo:
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

    @app.delete("/workspaces/{name}")
    def delete_workspace(
        name: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, str]:
        """Delete a managed world (destructive, irreversible). Loopback-only: a remote client must
        never be able to wipe whoever-owns-this-server's worlds."""
        if not _is_loopback(request):
            raise HTTPException(status_code=403, detail="删除世界仅限本机操作")
        try:
            delete_managed_world(name)
        except ValueError as e:
            raise HTTPException(
                status_code=404 if "不存在" in str(e) else 400, detail=str(e)
            ) from e
        return {"deleted": sanitize_world_name(name)}

    @app.get("/projects/{project}/overview")
    def project_overview(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
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
        return {
            "project": project,
            "inventory": build_content_inventory(_project_root_or_404(project)),
        }

    @app.get("/projects/{project}/readiness")
    def project_readiness(
        project: str,
        request: Request,
        kind: str | None = None,
        only_incomplete: bool = False,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Production-ready scoring of every content item (completeness, not correctness)."""
        return {
            "project": project,
            "readiness": build_readiness_report(
                _project_root_or_404(project),
                only_incomplete=only_incomplete,
                kind=kind,
            ),
        }

    @app.get("/projects/{project}/timeline")
    def project_timeline(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Chronology of quests + events with timeline violations marked (read-only, $0)."""
        return {
            "project": project,
            "timeline": build_timeline_view_model(_project_root_or_404(project)),
        }

    @app.get("/projects/{project}/graph")
    def project_graph(
        project: str,
        request: Request,
        focus: str = "",
        radius: int = 1,
        kinds: str | None = None,
        impact: bool = False,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Relationship subgraph (read-only). With ``focus`` → ego graph + optional impact ripple;
        without ``focus`` → whole-world clustered overview."""
        kind_set = {k.strip() for k in (kinds or "").split(",") if k.strip()} or None
        return {
            "project": project,
            "graph": build_graph_view_model(
                _project_root_or_404(project),
                focus_ref=focus or None,
                radius=max(1, min(radius, 3)),
                kinds=kind_set,
                impact=impact,
            ),
        }

    @app.get("/projects/{project}/relation_kinds")
    def project_relation_kinds(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """The pre-provided relationship-kind catalog the editor offers (custom still allowed)."""
        return {"project": project, **relation_kinds_view_model()}

    @app.get("/projects/{project}/dialogue_trees")
    def project_dialogue_trees(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """List the world's branching dialogue trees (read-only, $0)."""
        return {
            "project": project,
            "dialogues": build_dialogue_list_view_model(_project_root_or_404(project)),
        }

    @app.get("/projects/{project}/dialogue_trees/{tree_id}/flow")
    def project_dialogue_flow(
        project: str,
        tree_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Laid-out flow graph for one dialogue tree (read-only, $0)."""
        flow = build_dialogue_flow_view_model(_project_root_or_404(project), tree_id=tree_id)
        if flow is None:
            raise HTTPException(status_code=404, detail=f"dialogue tree {tree_id!r} not found")
        return {"project": project, "flow": flow}

    @app.get("/projects/{project}/dialogue_trees/{tree_id}")
    def project_dialogue_tree(
        project: str,
        tree_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Full structural dialogue tree for the editor (untruncated text/choices, read-only $0)."""
        tree = build_dialogue_tree_view_model(_project_root_or_404(project), tree_id=tree_id)
        if tree is None:
            raise HTTPException(status_code=404, detail=f"dialogue tree {tree_id!r} not found")
        return {"project": project, "tree": tree}

    @app.get("/projects/{project}/snapshots")
    def project_snapshots(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """List canon snapshots, newest first (read-only, $0)."""
        return {"project": project, **build_snapshots_view_model(_project_root_or_404(project))}

    @app.post("/projects/{project}/snapshots", status_code=201)
    def create_project_snapshot(
        project: str,
        req: SnapshotCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Take a labelled snapshot of the current world (writes under .snapshots/)."""
        meta = create_world_snapshot(_project_root_or_404(project), label=req.label)
        return {"project": project, "snapshot": meta}

    @app.get("/projects/{project}/diff")
    def project_diff(
        project: str,
        request: Request,
        from_id: str = Query(alias="from"),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Structural diff of a snapshot against the current world (read-only, $0)."""
        diff = build_diff_view_model(_project_root_or_404(project), from_id=from_id)
        if diff is None:
            raise HTTPException(status_code=404, detail=f"snapshot {from_id!r} not found")
        return {"project": project, "diff": diff}

    @app.post("/projects/{project}/jobs", response_model=JobCreatedResponse, status_code=202)
    def create_job(
        project: str,
        req: JobCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> JobCreatedResponse:
        """Run a long action asynchronously; progress streams over /jobs/{id}/events."""
        allowed = _JOB_PARAM_KEYS[req.kind]
        unknown = sorted(set(req.params) - allowed)
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown params for {req.kind}: {unknown}; allowed: {sorted(allowed)}",
            )
        if req.params.get("llm_mode") == "real":
            _require_real_allowed(request)
        content_root = _project_root_or_404(project)
        runners: dict[str, Callable[..., dict[str, Any]]] = {
            "world_seed": run_world_seed_action,
            "world_expand": run_world_expand_action,
            "extraction": run_extraction_action,
            "theme_sweep": run_theme_sweep_action,
            "character_profile": run_character_action,
            "build_overview": run_build_overview_action,
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
        result = list_review_items_action(_project_root_or_404(project))
        return ReviewItemsResponse(project=project, **result)

    @app.get("/projects/{project}/review/calibration")
    def review_calibration(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        result = reviewer_calibration_action(_project_root_or_404(project))
        return {"project": project, **result}

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

    @app.post(
        "/projects/{project}/review_items/{item_id}:revise",
        response_model=ReviewReviseResponse,
    )
    def revise_review_item_endpoint(
        project: str,
        item_id: str,
        req: ReviewReviseRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ReviewReviseResponse:
        """Feedback-driven revision: the reviewer asks for changes and the draft is regenerated in
        place, staying pending so a human still approves it (never an auto-land)."""
        if req.llm_mode == "real":
            _require_real_allowed(request)
        try:
            revised = revise_draft_action(
                _project_root_or_404(project),
                item_id=item_id,
                feedback=req.feedback,
                budget_tokens=req.budget_tokens,
                llm_mode=req.llm_mode,
                llm_model=req.llm_model,
            )
        except KeyError as e:
            raise HTTPException(status_code=404, detail=f"review item not found: {e}") from e
        except ValueError as e:
            raise _manage_error(e) from e
        return ReviewReviseResponse(
            project=project,
            item_id=item_id,
            item=revised.get("item") or {},
            revised_payload=revised.get("revised_payload") or {},
            cost_budget=revised.get("cost_budget") or {},
        )

    @app.post("/projects/{project}/contradictions:scan")
    def scan_contradictions(
        project: str,
        req: ContradictionScanRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Batch-2: find semantic contradictions in canon (judge confirms, else queued)."""
        if req.use_llm and req.llm_mode == "real":
            _require_real_allowed(request)
        result = detect_contradictions_action(
            _project_root_or_404(project),
            use_llm=req.use_llm,
            semantic_threshold=req.semantic_threshold,
            max_judge=req.max_judge,
            llm_mode=req.llm_mode,
            llm_model=req.llm_model or "deepseek-v4-flash",
        )
        return {"project": project, **result}

    @app.post("/projects/{project}/sweeps:run", response_model=ProjectSweepResponse)
    def run_theme_sweep(
        project: str,
        req: ProjectSweepRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> ProjectSweepResponse:
        if req.use_llm and req.llm_mode == "real":
            _require_real_allowed(request)
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
        try:
            result = update_entity_action(
                _project_root_or_404(project),
                entity_id=entity_id,
                name=req.name,
                description=req.description,
                tags=req.tags,
                metadata_updates=req.metadata_updates,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return EntityUpdateResponse(project=project, **result)

    @app.patch("/projects/{project}/style_guides/{guide_id}")
    def update_style_guide(
        project: str,
        guide_id: str,
        req: StyleGuidePatchRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """B10: inline-edit the worldview style guide (body + rules); lands at once (human edit)."""
        try:
            result = update_style_guide_action(
                _project_root_or_404(project),
                guide_id=guide_id,
                body=req.body,
                rules=req.rules,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

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

    # ---- graph/timeline/dialogue direct editing (same human-edit pipeline as PATCH entity) ----

    @app.post("/projects/{project}/entities", status_code=201)
    def create_entity(
        project: str,
        req: EntityCreateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = create_entity_action(
                _project_root_or_404(project),
                name=req.name,
                entity_type=req.type,
                description=req.description,
                tags=req.tags,
                metadata=req.metadata,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/quests/{quest_id}")
    def get_quest(
        project: str,
        quest_id: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Full quest (objective/prereqs absent from the timeline payload) for the editor ($0)."""
        quest = build_quest_view_model(_project_root_or_404(project), quest_id=quest_id)
        if quest is None:
            raise HTTPException(status_code=404, detail=f"quest {quest_id!r} not found")
        return {"project": project, "quest": quest}

    @app.patch("/projects/{project}/quests/{quest_id}")
    def patch_quest(
        project: str,
        quest_id: str,
        req: QuestPatchRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = update_quest_action(
                _project_root_or_404(project),
                quest_id=quest_id,
                title=req.title,
                objective=req.objective,
                timeline_order=req.timeline_order,
                set_timeline_order=req.set_timeline_order,
                prerequisites=req.prerequisites,
                giver_npc=req.giver_npc,
                location=req.location,
                if_match=req.if_match,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.patch("/projects/{project}/quests/{quest_id}/logic")
    def patch_quest_logic(
        project: str,
        quest_id: str,
        req: QuestLogicPatchRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Set/replace a quest's native logic layer; returns the deterministic logic issues."""
        try:
            result = update_quest_logic_action(
                _project_root_or_404(project), quest_id=quest_id, logic=req.logic
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/quests/{quest_id}/logic:draft")
    def draft_quest_logic_endpoint(
        project: str,
        quest_id: str,
        req: QuestLogicDraftRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """B7: AI-draft the quest's logic, gated by the deterministic audit, queued for review."""
        try:
            result = draft_quest_logic_action(
                _project_root_or_404(project),
                quest_id=quest_id,
                intent=req.intent,
                refine_rounds=req.refine_rounds,
                llm_mode=req.llm_mode,
                llm_model=req.llm_model or "deepseek-v4-flash",
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/quests/{quest_id}:simulate")
    def simulate_quest_endpoint(
        project: str,
        quest_id: str,
        req: QuestSimulateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-E playtest: walk the quest's logic and report path + outcome (deterministic, $0)."""
        try:
            result = simulate_quest_action(
                _project_root_or_404(project),
                quest_id=quest_id,
                choices=req.choices,
                initial_state=req.initial_state,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/search")
    def search_project(
        project: str,
        request: Request,
        q: str,
        kinds: str | None = None,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Global literal search across the canon (jump-to). $0, deterministic."""
        kind_list = [k for k in (kinds or "").split(",") if k] or None
        result = search_all_action(_project_root_or_404(project), query=q, kinds=kind_list)
        return {"project": project, **result}

    @app.post("/projects/{project}/rename:plan")
    def rename_plan(
        project: str,
        req: RenameRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Dry-run a rename — the reference edits + conflicts, mutating nothing."""
        try:
            result = plan_rename_action(
                _project_root_or_404(project),
                ref=req.ref,
                new_name=req.new_name,
                new_id=req.new_id,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/rename:apply")
    def rename_apply(
        project: str,
        req: RenameRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Apply a rename atomically (snapshot undo point + re-audit); returns the undo snapshot."""
        try:
            result = apply_rename_action(
                _project_root_or_404(project),
                ref=req.ref,
                operator=req.operator,
                new_name=req.new_name,
                new_id=req.new_id,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/snapshots:restore")
    def snapshots_restore(
        project: str,
        req: SnapshotRestoreRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Undo by restoring a snapshot (e.g. the undo point a rename returned)."""
        try:
            result = restore_snapshot_action(
                _project_root_or_404(project), snapshot_id=req.snapshot_id
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/collab")
    def collab_state(
        project: str,
        request: Request,
        object_ref: str | None = None,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """The collaboration ledger (assignments/comments/locks), optionally scoped to an object."""
        result = collab_state_action(_project_root_or_404(project), object_ref=object_ref)
        return {"project": project, **result}

    @app.get("/projects/{project}/analytics")
    def world_analytics(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-H deterministic world analytics dashboard (counts/density/gaps/coverage). $0."""
        result = world_analytics_action(_project_root_or_404(project))
        return {"project": project, **result}

    @app.get("/templates")
    def templates_library(
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-G · the built-in template / archetype library (project-independent)."""
        return list_templates_action()

    @app.post("/projects/{project}/templates:instantiate", status_code=201)
    def templates_instantiate(
        project: str,
        req: TemplateInstantiateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Stamp out content from a template + params, routed through the review queue."""
        try:
            result = instantiate_template_action(
                _project_root_or_404(project),
                template_id=req.template_id,
                params=req.params,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/collab/assign")
    def collab_assign(
        project: str,
        req: AssignRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = assign_action(
                _project_root_or_404(project),
                object_ref=req.object_ref,
                assignee=req.assignee,
                by=req.by,
                note=req.note,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/collab/comments", status_code=201)
    def collab_comment(
        project: str,
        req: CommentRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = comment_action(
                _project_root_or_404(project),
                object_ref=req.object_ref,
                author=req.author,
                body=req.body,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/collab/lock")
    def collab_lock(
        project: str,
        req: LockRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Acquire or release an edit lock; another holder's lock blocks (409)."""
        try:
            result = lock_action(
                _project_root_or_404(project),
                object_ref=req.object_ref,
                holder=req.holder,
                release=req.release,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/localization")
    def localization_overview(
        project: str,
        request: Request,
        locales: str | None = None,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-F · localization coverage + per-string status. $0."""
        loc_list = [loc for loc in (locales or "").split(",") if loc] or None
        result = localization_overview_action(_project_root_or_404(project), locales=loc_list)
        return {"project": project, **result}

    @app.post("/projects/{project}/localization:transition")
    def localization_transition(
        project: str,
        req: LocTransitionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = loc_transition_action(
                _project_root_or_404(project),
                text_key=req.text_key,
                locale=req.locale,
                to=req.to,
                by=req.by,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/localization:assign")
    def localization_assign(
        project: str,
        req: LocAssignRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = loc_assign_action(
                _project_root_or_404(project),
                text_key=req.text_key,
                locale=req.locale,
                assignee=req.assignee,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/compliance:scan")
    def compliance_scan(
        project: str,
        req: ComplianceScanRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Run the sweep with a rule pack and open/refresh remediation cases."""
        try:
            result = run_compliance_scan_action(
                _project_root_or_404(project),
                rule_pack=req.rule_pack,
                llm_mode=req.llm_mode,
                llm_model=req.llm_model or "deepseek-v4-flash",
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/compliance/report")
    def compliance_report(
        project: str,
        request: Request,
        rule_pack_id: str = "default",
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        result = compliance_report_action(_project_root_or_404(project), rule_pack_id=rule_pack_id)
        return {"project": project, **result}

    @app.post("/projects/{project}/compliance/cases/{case_id}:transition")
    def compliance_transition(
        project: str,
        case_id: str,
        req: CaseTransitionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = transition_case_action(
                _project_root_or_404(project),
                case_id=case_id,
                to=req.to,
                operator=req.operator,
                note=req.note,
                assignee=req.assignee,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/compliance/cases/{case_id}:rescan")
    def compliance_rescan(
        project: str,
        case_id: str,
        req: CaseRescanRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = rescan_case_action(
                _project_root_or_404(project),
                case_id=case_id,
                operator=req.operator,
                rule_pack=req.rule_pack,
                llm_mode=req.llm_mode,
                llm_model=req.llm_model or "deepseek-v4-flash",
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/assets")
    def assets_list(
        project: str,
        request: Request,
        object_ref: str | None = None,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-I · list media references attached to objects (or one object). $0."""
        result = asset_list_action(_project_root_or_404(project), object_ref=object_ref)
        return {"project": project, **result}

    @app.post("/projects/{project}/assets:attach", status_code=201)
    def assets_attach(
        project: str,
        req: AssetAttachRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Attach an existing media reference (uri) to an object; idempotent per ref|kind|uri."""
        try:
            result = asset_attach_action(
                _project_root_or_404(project),
                object_ref=req.object_ref,
                kind=req.kind,
                uri=req.uri,
                title=req.title,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/assets:detach")
    def assets_detach(
        project: str,
        request: Request,
        asset_id: str = Query(min_length=1),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        result = asset_detach_action(_project_root_or_404(project), asset_id=asset_id)
        return {"project": project, **result}

    @app.post("/projects/{project}/engine:import")
    def engine_import(
        project: str,
        req: EngineImportRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-K · pull engine-side quest rows back: diff vs canon, queue new/changed for review."""
        try:
            result = import_from_engine_action(_project_root_or_404(project), quests=req.quests)
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/import:recognize")
    def import_recognize(
        project: str,
        req: RecognizeRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-R · recognize a foreign project file (table/articy/ink/yarn/ue/unity) into an editable
        plan; with apply=true, stage new/changed into review + return a pre-merge audit preview."""
        try:
            result = recognize_content_action(
                _project_root_or_404(project),
                source_format=req.source_format,
                content=req.content,
                content_base64=req.content_base64,
                filename=req.filename,
                field_mapping=req.field_mapping,
                apply=req.apply,
                enable_llm=req.enable_llm,
                llm_mode=_llm_mode(),
                operator=req.operator,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/import:apply")
    def import_apply(
        project: str,
        req: RecognizeApplyRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """WS-R · stage an edited ImportPlan (human kept/dropped proposals) into review."""
        try:
            result = recognize_apply_plan_action(
                _project_root_or_404(project), plan=req.plan, operator=req.operator
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.get("/projects/{project}/recognize/mappings")
    def recognize_mappings_list(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        return {"project": project, **list_mapping_templates_action(_project_root_or_404(project))}

    @app.post("/projects/{project}/recognize/mappings")
    def recognize_mappings_save(
        project: str,
        req: MappingTemplateRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = save_mapping_template_action(
                _project_root_or_404(project), name=req.name, mapping=req.mapping
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.delete("/projects/{project}/recognize/mappings/{name}")
    def recognize_mappings_delete(
        project: str,
        name: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        result = delete_mapping_template_action(_project_root_or_404(project), name=name)
        return {"project": project, **result}

    @app.post("/projects/{project}/relations", status_code=201)
    def add_relation(
        project: str,
        req: RelationRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = add_relation_action(
                _project_root_or_404(project),
                source=req.source,
                target=req.target,
                kind=req.kind,
                symmetric=req.symmetric,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.delete("/projects/{project}/relations")
    def remove_relation(
        project: str,
        request: Request,
        source: str = Query(),
        target: str = Query(),
        kind: str = Query(),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = remove_relation_action(
                _project_root_or_404(project), source=source, target=target, kind=kind
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.patch("/projects/{project}/dialogue_trees/{tree_id}")
    def patch_dialogue_tree(
        project: str,
        tree_id: str,
        req: DialogueTreePatchRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = update_dialogue_tree_action(
                _project_root_or_404(project),
                tree_id=tree_id,
                title=req.title,
                root_node=req.root_node,
                nodes=req.nodes,
                metadata_updates=req.metadata_updates,
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/graph/positions")
    def set_node_position(
        project: str,
        req: NodePositionRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        try:
            result = set_object_position_action(
                _project_root_or_404(project), ref=req.ref, x=req.x, y=req.y
            )
        except ValueError as e:
            raise _manage_error(e) from e
        return {"project": project, **result}

    @app.post("/projects/{project}/exports", response_model=ProjectExportResponse)
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

    @app.get("/projects/{project}/lorebook")
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

    @app.get("/projects/{project}/references")
    def list_references(
        project: str,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        return list_references_action(_project_root_or_404(project))

    @app.post("/projects/{project}/references", status_code=201)
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

    @app.post("/projects/{project}/references:search")
    def search_references(
        project: str,
        req: ReferenceSearchRequest,
        request: Request,
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        return search_references_action(
            _project_root_or_404(project), query=req.query, budget_tokens=req.budget_tokens
        )

    @app.post("/projects/{project}/ingest")
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
    @app.get("/platform/me")
    def platform_me(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        return _principal(authorization, x_api_key).model_dump(mode="json")

    @app.post("/platform/tenants", status_code=201)
    def platform_create_tenant(
        req: TenantCreateRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _principal(authorization, x_api_key)
        _require_role_http(principal, Role.OWNER)
        owner = User(id=f"{req.tenant_id}_owner", email=req.owner_email)
        platform_store.create_tenant(Tenant(id=req.tenant_id, name=req.name))
        platform_store.create_user(owner)
        platform_store.add_membership(
            Membership(tenant_id=req.tenant_id, user_id=owner.id, role=Role.OWNER)
        )
        platform_store.record(
            AuditEntry(
                tenant_id=req.tenant_id,
                user_id=principal.user_id,
                action="tenant.create",
                target=req.tenant_id,
            )
        )
        return {"tenant_id": req.tenant_id, "owner_user_id": owner.id}

    @app.post("/platform/memberships", status_code=201)
    def platform_add_member(
        req: MembershipRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _principal(authorization, x_api_key)
        _require_role_http(principal, Role.OWNER)
        try:
            platform_store.create_user(User(id=req.user_id, email=req.email))
        except Exception:  # noqa: BLE001 — user may already exist; membership is the point
            pass
        platform_store.add_membership(
            Membership(tenant_id=req.tenant_id, user_id=req.user_id, role=Role(req.role))
        )
        return {"tenant_id": req.tenant_id, "user_id": req.user_id, "role": req.role}

    @app.post("/platform/auth/dev-token")
    def platform_dev_token(
        req: DevTokenRequest,
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        """Mint a bearer token (dev/test; production uses the OIDC provider). Owner-gated."""
        principal = _principal(authorization, x_api_key)
        _require_role_http(principal, Role.OWNER)
        try:
            token = mint_token(
                Principal(user_id=req.user_id, tenant_id=req.tenant_id, role=Role(req.role)),
                ttl_seconds=req.ttl_seconds,
            )
        except AuthError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return {"token": token, "token_type": "Bearer"}

    @app.get("/platform/audit")
    def platform_audit(
        request: Request,
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> dict[str, Any]:
        principal = _principal(authorization, x_api_key)
        return {
            "tenant_id": principal.tenant_id,
            "entries": [
                e.model_dump(mode="json")
                for e in platform_store.audit_for_tenant(principal.tenant_id)
            ],
        }

    if frontend_dist is not None:
        # One process, one port: the built Vue app ships from the API itself, so launch
        # is a single command with no CORS and no env vars. Mounted last — API routes
        # registered above keep precedence.
        app.mount("/", _SpaStaticFiles(directory=str(frontend_dist), html=True), name="spa")

    return app


def _require_role_http(principal: Principal, minimum: Role) -> None:
    try:
        require_role(principal, minimum)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


app = create_app()
