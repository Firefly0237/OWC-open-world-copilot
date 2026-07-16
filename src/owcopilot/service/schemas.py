# 服务层请求/响应模型（Pydantic DTO）与作业常量：REST 契约的数据形状集中处。
# Request/response Pydantic models plus job constants for the v2 content-workbench API.
"""Data-transfer models shared by the composition root and every domain router.

These are pure schema declarations (no behaviour): moving them out of ``api.py`` lets each
router import exactly the request/response shapes it serves without dragging in the app factory.
The REST contract — field names, validators, defaults — is unchanged from when these lived in
``api.py``; the API tests pin that contract.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from ..content.models import ContentBundle
from ..exporters import EngineTarget


# --------------------------------------------------------------------------- request / response
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
    # Opt-in RAGAS-style entailment gate (annotate, not block). off (default): unchanged. offline:
    # deterministic $0 lexical judge. llm: the answering model judges each claim (needs llm_mode
    # real). The result rides on answer.faithfulness; it never suppresses or rewrites the answer.
    faithfulness: str = Field(default="off", pattern="^(off|offline|llm)$")


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
    glean_rounds: int = Field(default=1, ge=0, le=5)
    verify_faithfulness: bool = False


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
    # UNITY target only: also emit the opt-in Unity Localization String Table Collection CSV under
    # unity/localization/. Additive; default False keeps the export byte-for-byte as before.
    unity_localization: bool = Field(default=False)


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


# IN-4: Response models for GET /projects/{p}/review_items/{id}:context
class PayloadStageSummary(BaseModel):
    id: str
    description: str


class PayloadSummary(BaseModel):
    title: str | None = None
    objective: str | None = None
    stages: list[PayloadStageSummary] = []
    summary: str | None = None


class CalibrationContextResp(BaseModel):
    item_type: str
    false_pass_rate: float | None = None
    sample_size: int
    sufficient_sample: bool


class ReviewItemContextResponse(BaseModel):
    item_id: str
    item_type: str
    status: str
    payload_summary: PayloadSummary
    issue_refs: list[str] = []
    critic_verdict: str | None = None
    critic_score: float | None = None
    refine_trail_last_reflection: str | None = None
    calibration_context: CalibrationContextResp


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
    "world_seed": {
        "brief",
        "llm_mode",
        "llm_model",
        "budget_tokens",
        "refine_rounds",
        "premise_candidates",
    },
    "world_expand": {"brief", "llm_mode", "llm_model", "budget_tokens", "refine_rounds"},
    "extraction": {
        "title",
        "text",
        "source_kind",
        "glean_rounds",
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
