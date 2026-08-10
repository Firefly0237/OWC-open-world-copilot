# 服务层请求/响应模型（Pydantic DTO）与作业常量：REST 契约的数据形状集中处。
# Request/response Pydantic models plus job constants for the v2 content-workbench API.
"""Data-transfer models shared by the composition root and every domain router.

These are pure schema declarations (no behaviour): moving them out of ``api.py`` lets each
router import exactly the request/response shapes it serves without dragging in the app factory.
The REST contract — field names, validators, defaults — is unchanged from when these lived in
``api.py``; the API tests pin that contract.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..content.models import ContentBundle
from ..exporters import EngineTarget
from ..inspiration.models import Provenance
from ..platform.models import TenantId
from ..spec_authoring import SpecDraft, TranslationOutcome

ReferenceUse = Literal["inspiration", "drafting"]


def _default_reference_uses() -> list[ReferenceUse]:
    return ["inspiration"]


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
    total: int = 0
    next_cursor: str | None = None
    snapshot_etag: str = ""
    cost_budget: dict[str, Any]


class ProjectContextPackRequest(ProjectContentRequest):
    query: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)


class ProjectContextPackResponse(BaseModel):
    request_id: str
    project: str
    refs: list[str]
    hits: list[dict[str, Any]]
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    cost_budget: dict[str, Any]


class ProjectAskRequest(ProjectContentRequest):
    query: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)
    max_cost_usd: float | None = Field(default=None, ge=0)
    llm_mode: str = Field(default="offline", pattern="^(offline|real)$")
    llm_model: str = Field(default="deepseek-v4-flash", max_length=120)
    agentic: bool = False
    # Opt-in RAGAS-style entailment gate (annotate, not block). off (default): unchanged. offline:
    # deterministic $0 lexical judge. llm: the answering model judges each claim (needs llm_mode
    # real). The result rides on answer.faithfulness; it never suppresses or rewrites the answer.
    faithfulness: str = Field(default="off", pattern="^(off|offline|llm)$")


class ProjectAskResponse(BaseModel):
    request_id: str
    project: str
    answer: dict[str, Any]
    degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
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
    # Rights declaration for the manuscript. This endpoint produces canon drafts, so
    # third_party_reference is refused (422) and pointed at the inspiration library.
    provenance: Provenance = Provenance.OWN_IP


class ProjectExtractionRunResponse(BaseModel):
    request_id: str
    project: str
    draft: dict[str, Any]
    intake_token: str
    stats: dict[str, int]
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectExtractionSubmitRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    draft: dict[str, Any]
    intake_token: str = Field(min_length=1, max_length=256)
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
    # N4.3: 额外从灵感库取参考（默认关；只召回 allowed_uses 含 drafting 的素材）。
    use_references: bool = False


class ProjectDialogueTreeResponse(BaseModel):
    request_id: str
    project: str
    tree: dict[str, Any]
    lint_issues: list[dict[str, Any]]
    structure_problems: list[str]
    refine_trail: list[dict[str, Any]] = Field(default_factory=list)
    auto_review_incomplete: bool = False
    # 灵感库那一侧的溯源；``use_references=False`` 或库里没有可用素材时恒为空列表。
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[dict[str, Any]] = Field(default_factory=list)
    review_item_id: str | None
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectFlavorRequest(_LLMModeRequest):
    category: str = Field(pattern="^(item|skill|achievement)$")
    names: list[str] = Field(min_length=1, max_length=50)
    theme: str = Field(default="", max_length=200)
    max_chars: int = Field(default=120, ge=20, le=400)
    refine_rounds: int = Field(default=0, ge=0, le=4)
    # N4.3: 额外从灵感库取参考（默认关；只召回 allowed_uses 含 drafting 的素材）。
    use_references: bool = False


class ProjectFlavorResponse(BaseModel):
    request_id: str
    project: str
    batch_id: str
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    # 灵感库那一侧的溯源；``use_references=False`` 或库里没有可用素材时恒为空列表。
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[dict[str, Any]] = Field(default_factory=list)
    review_item_id: str | None
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectDraftRequest(_LLMModeRequest):
    brief: str = Field(min_length=1, max_length=4000)
    budget_tokens: int = Field(default=800, ge=1, le=8000)
    refine_rounds: int = Field(default=2, ge=0, le=4)
    use_references: bool = False


class ProjectDraftResponse(BaseModel):
    request_id: str
    project: str
    quest: dict[str, Any]
    issues: list[dict[str, Any]]
    refine_trail: list[dict[str, Any]] = Field(default_factory=list)
    auto_review_incomplete: bool = False
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[dict[str, Any]] = Field(default_factory=list)
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
    # N4.3: 额外从灵感库取参考（默认关；只召回 allowed_uses 含 drafting 的素材）。
    use_references: bool = False


class ProjectBarksResponse(BaseModel):
    request_id: str
    project: str
    accepted: list[dict[str, Any]]
    rejected: list[dict[str, Any]]
    # 灵感库那一侧的溯源；``use_references=False`` 或库里没有可用素材时恒为空列表。
    inspiration_refs: list[str] = Field(default_factory=list)
    reference_report: list[dict[str, Any]] = Field(default_factory=list)
    review_item_ids: list[str]
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class ProjectExportRequest(BaseModel):
    target_engine: str = Field(default=EngineTarget.GENERIC.value)
    # Creator-facing planning deliverables generated beside the engine projection.
    planning_workbook: bool = Field(default=False)
    planning_notes: bool = Field(default=False)
    # UNITY target only: also emit the opt-in Unity Localization String Table Collection CSV under
    # unity/localization/. Additive; default False keeps the export byte-for-byte as before.
    unity_localization: bool = Field(default=False)
    # UNREAL target only: rename DataTable columns to the target project's own USTRUCT field names,
    # keyed by table ({"<table>": {"<CanonicalField>": "<ExportName>"}}). None/omit keeps the
    # default field names, so the export stays byte-for-byte as before.
    row_struct_map: dict[str, dict[str, str]] | None = Field(default=None)


class ProjectExportResponse(BaseModel):
    request_id: str
    project: str
    output_dir: str
    manifest: dict[str, Any]
    cost_budget: dict[str, Any]


class ProseCheckRequest(BaseModel):
    """A pasted chapter to check against the archive. Deterministic, no LLM (``check_prose`` is a
    pure $0 rule pass), so this carries no ``llm_mode``/``use_llm`` knob unlike the sibling
    generation requests."""

    text: str = Field(min_length=1, max_length=200_000)


class ProseCheckResponse(BaseModel):
    request_id: str
    project: str
    resolved_mentions: list[dict[str, Any]]
    issues: list[dict[str, Any]]
    stats: dict[str, int]
    cost_budget: dict[str, Any]


# ---- round-13 platform endpoints: workspaces / review decisions / sweep / manage ----
class WorkspaceInfo(BaseModel):
    name: str
    path: str


class WorkspaceListResponse(BaseModel):
    workspaces: list[WorkspaceInfo]
    total: int | None = None
    next_cursor: str | None = None
    snapshot_etag: str | None = None


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
    if_match: str | None = None  # same optimistic-concurrency guard as the normal quest editor


class QuestSimulateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    choices: list[str] | None = Field(default=None, max_length=200)
    initial_state: dict[str, bool | int | str] | None = Field(default=None, max_length=500)
    dialogue_tree_id: str | None = Field(default=None, min_length=1, max_length=200)
    dialogue_choices: list[str] | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def dialogue_choices_require_tree(self) -> QuestSimulateRequest:
        if self.dialogue_choices and not self.dialogue_tree_id:
            raise ValueError("dialogue_choices 需要同时提供 dialogue_tree_id")
        return self


class QuestSimulateStepResponse(BaseModel):
    stage_id: str
    precondition_ok: bool
    effects: list[str] = Field(default_factory=list)
    branch_taken: str = ""


class QuestSimulateRunResponse(BaseModel):
    status: str
    path: list[str] = Field(default_factory=list)
    steps: list[QuestSimulateStepResponse] = Field(default_factory=list)
    final_state: dict[str, bool | int | str] = Field(default_factory=dict)
    message: str = ""


class QuestDialogueWalkStepResponse(BaseModel):
    node_id: str
    chosen: str = ""
    effects: list[str] = Field(default_factory=list)


class QuestDialogueWalkResponse(BaseModel):
    tree_id: str
    status: str
    path: list[str] = Field(default_factory=list)
    steps: list[QuestDialogueWalkStepResponse] = Field(default_factory=list)
    final_state: dict[str, bool | int | str] = Field(default_factory=dict)
    message: str = ""


class QuestSimulateResponse(BaseModel):
    project: str
    quest_id: str
    run: QuestSimulateRunResponse
    dialogue_walk: QuestDialogueWalkResponse | None = None
    cost_budget: dict[str, Any]


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
    baseline_quests: list[dict[str, Any]] = Field(min_length=1)


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
    tenant_id: TenantId
    name: str = Field(min_length=1)
    owner_email: str = Field(min_length=3)


class MembershipRequest(BaseModel):
    tenant_id: TenantId
    user_id: str = Field(min_length=1)
    email: str = Field(min_length=3)
    role: Literal["owner", "editor", "reviewer", "viewer"] = "editor"


class DevTokenRequest(BaseModel):
    user_id: str = Field(min_length=1)
    tenant_id: TenantId
    role: Literal["owner", "editor", "reviewer", "viewer"] = "editor"
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
    z: float | None = None


class WorkspaceImportRequest(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    zip_base64: str = Field(min_length=4)


class ReviewItemsResponse(BaseModel):
    project: str
    count: int
    items: list[dict[str, Any]]
    next_cursor: str | None = None
    snapshot_etag: str = ""
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


class LessonRevokeRequest(BaseModel):
    reason: str = Field(default="", max_length=500)


class ProjectSweepRequest(BaseModel):
    theme: str = Field(min_length=1, max_length=200)
    extra_terms: list[str] = Field(default_factory=list)
    use_llm: bool = False
    llm_mode: str = "offline"
    llm_model: str = "deepseek-v4-flash"
    max_judge: int = Field(default=400, ge=1, le=2000)
    semantic_threshold: float = Field(default=0.5, ge=0.0, le=1.0, allow_inf_nan=False)


class SweepDismissRequest(BaseModel):
    """逐一排除 one sweep finding. ``finding_id`` (the 排除码 a prior sweep response emitted) is the
    stable dismissal key; ``ref`` / ``locator`` / ``layer`` are the audit trail persisted with it.
    ``operator`` is the required signature."""

    theme: str = Field(min_length=1, max_length=200)
    finding_id: str = Field(min_length=1, max_length=80)
    ref: str = Field(min_length=1, max_length=400)
    locator: str = Field(default="", max_length=400)
    layer: str = Field(default="", max_length=40)
    operator: str = Field(min_length=1, max_length=200)
    reason: str = Field(default="", max_length=2000)


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
    # Additive defaults keep callers that construct this response directly compatible while the
    # REST surface now exposes the semantic coverage already returned by the underlying action.
    semantic_used: bool = False
    semantic_flagged: int = 0
    judged_count: int
    judge_skipped: int
    hits: list[dict[str, Any]]
    review_suggested: list[dict[str, Any]]
    # findings past dismissals suppressed this run (逐一排除 honesty counter); defaulted so the
    # field is additive and older clients keep working.
    dismissed_count: int = 0
    markdown: str
    cost_budget: dict[str, Any]


class ReferenceAddRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    # A whole reference book is fine: the inspiration store chunks + BM25-indexes it, and genesis
    # retrieves only the budget-bounded relevant chunks — so length never overruns a model call.
    text: str = Field(min_length=1, max_length=2_000_000)
    source_type: str = Field(default="uploaded_file", max_length=40)
    original_filename: str | None = Field(default=None, max_length=200)
    # The library takes all three provenances (it is physically separate from canon); the default
    # is the most conservative one.
    provenance: Provenance = Provenance.THIRD_PARTY_REFERENCE
    # Inspiration is the safe backwards-compatible default. Drafting must be an explicit uploader
    # grant because task/character/dialogue chains can place the retrieved text in a model prompt.
    allowed_uses: list[ReferenceUse] = Field(
        default_factory=_default_reference_uses, min_length=1, max_length=2
    )


class ReferenceUploadRequest(BaseModel):
    """Binary upload branch (N2.3 / B6): the browser can only hand us bytes for a .docx/.pdf/.epub,
    never extracted plain text, so this is a separate shape from ``ReferenceAddRequest`` rather than
    an optional field bolted onto it — the plain-text door's contract stays exactly what it was."""

    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=200)
    # JSON/base64 has no multipart Content-Type. Clients may still declare one; the formal intake
    # boundary then verifies it against the extension. Omitted keeps older clients compatible.
    mime_type: str | None = Field(default=None, min_length=1, max_length=200)
    content_base64: str = Field(min_length=1)
    provenance: Provenance = Provenance.THIRD_PARTY_REFERENCE
    allowed_uses: list[ReferenceUse] = Field(
        default_factory=_default_reference_uses, min_length=1, max_length=2
    )


class ReferenceUploadArtifactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    artifact_version_id: str
    content_hash: str
    byte_size: int
    mime_type: str
    raw_preserved: bool
    locators_preserved: bool


class ReferenceUploadResponse(BaseModel):
    """Honest upload result: callers can distinguish exact locators from flattened compatibility."""

    model_config = ConfigDict(extra="forbid")

    source: dict[str, Any]
    chunks: list[dict[str, Any]]
    indexed_count: int
    injection_flagged_chunks: list[str]
    ingestion_mode: Literal["structured_locator", "legacy_text", "flattened_binary"]
    artifact: ReferenceUploadArtifactResponse | None = None
    degraded: bool
    degradation_reasons: list[str] = Field(default_factory=list)
    cost_budget: dict[str, Any]


class ReferenceApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    allowed_uses: list[ReferenceUse] = Field(
        default_factory=_default_reference_uses, min_length=1, max_length=2
    )
    note: str = Field(default="", max_length=500)


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
    "agent_diagnose",
    "multi_agent",
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
        "provenance",
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
    # E1: the ReAct diagnosis run. Keys mirror ``run_agent_diagnose_action``'s keyword parameters
    # (the job runner splats them straight in). ``thread_id``/``resume``/``decision`` are how a run
    # PAUSED at the human-approval gate is continued — the same interrupt/resume mechanics the CLI
    # uses, carried over the job surface rather than a second suspend protocol. Deliberately NOT
    # exposed: ``chat_model`` (an in-process injection point) and ``sqlite_path`` /
    # ``checkpoint_path`` (filesystem paths never travel in a v2 request body).
    "agent_diagnose": {
        "goal",
        "max_steps",
        "max_model_calls",
        "max_cost_usd",
        "max_tool_calls",
        "skills",
        "recall",
        "hitl",
        "early_stopping",
        "thread_id",
        "resume",
        "decision",
        "decision_message",
        "llm_mode",
        "llm_model",
        "tool_transport",
    },
    "multi_agent": {
        "goal",
        "max_tool_calls",
        "max_total_tool_calls",
        "max_model_calls",
        "max_cost_usd",
        "llm_mode",
        "llm_model",
    },
}


class ConnectionStatusResponse(BaseModel):
    configured: bool
    base_url: str = ""
    provider: Literal["deepseek", "openai_compatible", "anthropic"] = "deepseek"
    # whether real per-tier prices are set (OWCOPILOT_PRICE_*); when false the reported cost is a
    # ballpark estimate from illustrative prices, which the UI labels accordingly.
    prices_configured: bool = False


class ConnectionUpdateRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    provider: Literal["deepseek", "openai_compatible", "anthropic"] | None = None


class ConnectionProbeRequest(BaseModel):
    base_url: str = ""
    api_key: str = ""
    model: str = Field(min_length=1, max_length=120)
    provider: Literal["deepseek", "openai_compatible", "anthropic"] | None = None


_JOB_KIND_PATTERN = "^(" + "|".join(_JOB_KINDS) + ")$"


class MultiAgentJobParams(BaseModel):
    """Typed REST contract for the cost-amplifying multi-agent job kind."""

    model_config = ConfigDict(extra="forbid")

    goal: str = Field(min_length=1, max_length=1200)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100)
    max_total_tool_calls: int | None = Field(default=None, ge=1, le=4096)
    max_model_calls: int | None = Field(default=None, ge=1, le=512)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    llm_mode: str = Field(default="offline", pattern="^(offline|real)$")
    llm_model: str | None = Field(default=None, min_length=1, max_length=120)

    @field_validator("goal")
    @classmethod
    def goal_must_not_be_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("multi_agent params.goal 不能为空")
        return normalized


class AgentDiagnoseJobParams(BaseModel):
    """Typed REST boundary for Agent controls while preserving worker-side misuse guidance."""

    # Unknown keys remain visible so ``create_job`` can keep its established actionable 400.
    model_config = ConfigDict(extra="allow")

    goal: str = Field(default="", max_length=1200)
    max_steps: int | None = Field(default=None, ge=1, le=100)
    max_model_calls: int | None = Field(default=None, ge=1, le=100)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    max_tool_calls: int | None = Field(default=None, ge=1, le=100)
    skills: list[str] | None = Field(default=None, max_length=100)
    recall: bool | None = None
    hitl: bool | None = None
    early_stopping: str | None = Field(default=None, pattern="^(force|generate)$")
    thread_id: str | None = Field(default=None, max_length=200)
    resume: bool | None = None
    decision: str | None = Field(default=None, pattern="^(approve|reject)$")
    decision_message: str | None = Field(default=None, max_length=2000)
    llm_mode: str | None = Field(default=None, pattern="^(offline|real)$")
    llm_model: str | None = Field(default=None, min_length=1, max_length=120)
    tool_transport: str | None = Field(default=None, pattern="^(native|mcp)$")


class JobCreateRequest(BaseModel):
    kind: str = Field(pattern=_JOB_KIND_PATTERN)
    params: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_job_params(self) -> JobCreateRequest:
        """Type-check costly Agent controls before an asynchronous job is accepted."""
        if self.kind == "multi_agent":
            multi_params = MultiAgentJobParams.model_validate(self.params)
            self.params = multi_params.model_dump(exclude_none=True, exclude_unset=True)
            return self
        if self.kind == "agent_diagnose":
            agent_params = AgentDiagnoseJobParams.model_validate(self.params)
            self.params = agent_params.model_dump(exclude_none=True, exclude_unset=True)
        return self


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


# --------------------------------------------------------------------------- engineering lab
class EngineeringCatalogResponse(BaseModel):
    project: str
    quest_ids: list[str]
    dialogue_tree_ids: list[str]
    style_rubrics: list[dict[str, Any]]
    kind: Literal["quest", "dialogue_tree", "style_rubric"] | None = None
    total: int | None = None
    next_cursor: str | None = None
    snapshot_etag: str = ""
    cost_budget: dict[str, Any]


class QuestExploreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_states: int = Field(default=100_000, ge=1, le=1_000_000)
    max_depth: int = Field(default=10_000, ge=1, le=100_000)


class QuestExploreResponse(BaseModel):
    request_id: str
    project: str
    quest_id: str
    report: dict[str, Any]
    execution_mode: Literal["deterministic_bounded_search"]
    cost_budget: dict[str, Any]


class TemporalSpecTranslateRequest(BaseModel):
    """Step 1: produce an auditable candidate; never enqueue or mutate canon."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=1200)
    use_llm: bool = False
    llm_mode: Literal["offline", "real"] = "offline"
    llm_model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=120)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)

    @model_validator(mode="after")
    def real_model_requires_explicit_opt_in(self) -> TemporalSpecTranslateRequest:
        if self.use_llm and self.llm_mode != "real":
            raise ValueError("use_llm=true requires llm_mode=real")
        return self


class TemporalSpecTranslateResponse(BaseModel):
    request_id: str
    project: str
    quest_id: str
    outcome: TranslationOutcome
    translator_mode: Literal["deterministic", "real_model", "real_model_fallback"]
    used_llm: bool
    degraded: bool
    degradation_reasons: list[str] = Field(default_factory=list)
    telemetry: dict[str, Any]
    writes_canon: Literal[False]
    cost_budget: dict[str, Any]


class TemporalSpecSubmitRequest(BaseModel):
    """Step 2: replay a structural draft server-side, then queue it for explicit review."""

    model_config = ConfigDict(extra="forbid")

    intent: str = Field(min_length=1, max_length=1200)
    draft: SpecDraft


class TemporalSpecSubmitResponse(BaseModel):
    request_id: str
    project: str
    quest_id: str
    review_item_id: str
    review_item: dict[str, Any]
    outcome: TranslationOutcome
    writes_canon: Literal[False]
    cost_budget: dict[str, Any]


class FlagXrefResponse(BaseModel):
    project: str
    flags: list[dict[str, Any]]
    reads_complete: bool
    unparsable: list[dict[str, Any]]
    summary: dict[str, Any]
    cost_budget: dict[str, Any]


class DialogueWalkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["walk", "explore"] = "walk"
    choices: list[str] = Field(default_factory=list, max_length=200)
    initial_state: dict[str, bool | int | str] = Field(default_factory=dict)
    max_endings: int = Field(default=1_000, ge=1, le=10_000)


class DialogueWalkResponse(BaseModel):
    request_id: str
    project: str
    tree_id: str
    mode: Literal["walk", "explore"]
    result: dict[str, Any]
    cost_budget: dict[str, Any]


class RepairPlanRequest(BaseModel):
    """Bounded MCTS search with optional real-model candidate widening.

    ``llm_candidates`` is a no-op in offline mode, matching the CLI: deterministic fixers remain
    the safe/default move source.  ``max_cost_usd`` is reported as a post-run settlement threshold;
    the hard dispatch bound for this workflow is ``max_llm_calls``.
    """

    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=200, ge=0, le=2_000)
    max_depth: int | None = Field(default=None, ge=1, le=100)
    seed: int = Field(default=0, ge=-(2**31), le=2**31 - 1)
    llm_candidates: bool = False
    max_llm_calls: int = Field(default=32, ge=1, le=128)
    llm_mode: Literal["offline", "real"] = "offline"
    llm_model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=120)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class RepairPlanResponse(BaseModel):
    request_id: str
    project: str
    plan: dict[str, Any]
    candidate_source: Literal["deterministic_fixers", "deterministic_plus_model"]
    used_llm: bool
    llm_mode: Literal["offline", "real"]
    telemetry: dict[str, Any]
    writes_canon: Literal[False]
    cost_budget: dict[str, Any]


class QualityHarnessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    propose_fixes: bool = True
    max_issues: int = Field(default=5, ge=0, le=50)
    max_candidates_per_issue: int = Field(default=1, ge=0, le=5)


class QualityHarnessResponse(BaseModel):
    request_id: str
    project: str
    content_hash: str
    phase: str
    stop_reason: str
    export_ready: bool
    audit_totals: dict[str, int]
    top_issues: list[dict[str, Any]]
    export_blockers: list[dict[str, str]]
    readiness: dict[str, Any]
    patch_proposals: list[dict[str, Any]]
    next_tool_calls: list[dict[str, Any]]
    tool_trace: list[str]
    writes_canon: Literal[False]
    cost_budget: dict[str, Any]


class StyleSweepRequest(BaseModel):
    """StyleRubric cascade with an optional, explicitly bounded L3 judge."""

    model_config = ConfigDict(extra="forbid")

    semantic_threshold: float = Field(default=0.5, ge=0, le=1, allow_inf_nan=False)
    use_llm: bool = False
    max_judge: int = Field(default=400, ge=1, le=2_000)
    llm_mode: Literal["offline", "real"] = "offline"
    llm_model: str = Field(default="deepseek-v4-flash", min_length=1, max_length=120)
    max_cost_usd: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class StyleSweepResponse(BaseModel):
    request_id: str
    project: str
    rubric_id: str
    rubric_label: str
    rubric_version: str
    rubric_fingerprint: str
    abstract_tag: str
    scanned_total: int
    scanned_by_kind: dict[str, int]
    scanned_fields: int
    semantic_used: bool
    semantic_threshold: float
    llm_used: bool
    judge_mode: Literal["disabled", "offline_double", "real_model"]
    judge_limit: int
    judged_count: int
    judge_skipped: int
    dismissed_count: int
    l1_total: int
    l2_total: int
    l1_l2_overlap: int
    hits: list[dict[str, Any]]
    review_suggested: list[dict[str, Any]]
    elements: list[dict[str, Any]]
    markdown: str
    telemetry: dict[str, Any]
    cost_budget: dict[str, Any]


class AgentRunSummary(BaseModel):
    id: str
    goal: str
    stop_reason: str
    step_count: int
    cost_usd: float
    latency_ms: float
    agent_name: str
    created_at: str


class AgentRunsResponse(BaseModel):
    project: str
    runs: list[AgentRunSummary]
    total: int
    next_cursor: str | None = None
    previous_cursor: str | None = None
    page_cursor: str | None = None
    snapshot_etag: str
    cost_budget: dict[str, Any]


class AgentRunDetail(AgentRunSummary):
    transcript: dict[str, Any]


class AgentRunDetailResponse(BaseModel):
    project: str
    run: AgentRunDetail
    cost_budget: dict[str, Any]
