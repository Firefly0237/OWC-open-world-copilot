import { apiGet, apiPost, currentProject } from "./api";
import { evolutionClient } from "./evolutionStudioApi";
import type { EvolutionGraph, EvolutionRunState, EvolutionQuest, IntentContractRecord, IntentReview } from "./evolutionStudioApi";
import type { StudioTask, StudioPreview } from "./studioApi";
import type { EvolutionArtifact, IntentApplicationReceipt } from "./evolutionStudioApi";

export type CollaborationMode = "delegate" | "direction" | "edit";
export interface CollaborationPolicy {
  mode: CollaborationMode;
  plan_review?: boolean;
  key_step_review?: boolean;
  allow_read_tools?: boolean;
  allow_candidate_trial?: boolean;
  allowed_operations?: string[] | null;
  review_operations?: string[];
}
export interface ApplicationContinuation {
  reading_path?: string;
  task_preview_id?: string; task_preview_seq?: number;
  narrative_project_id?: string; narrative_scene_id?: string; narrative_project_hash?: string;
  project_preview_id?: string; project_preview_hash?: string;
}
export interface ApplicationDetail {
  receipt: IntentApplicationReceipt; artifact: EvolutionArtifact; task: StudioTask;
  impact: Record<string, unknown>; target_current: boolean;
}
export interface MethodTrialScope {
  permit: { id: string; permit_hash: string; task_id: string; task_revision: number;
    allowed_versions: Record<string, string>; requirements?: MachineRequirements;
    authority: string; reason: string; author_acceptance: string };
  status: string; events: Record<string, unknown>[];
}
export interface PreviewCompatibility {
  status: "compatible" | "incompatible"; reasons: string[];
  evidence: Record<string, unknown>; recovery_actions: unknown[];
}
export interface CreatorSource {
  id: string; title: string; allowed_uses: string[]; char_count?: number;
  chunk_count?: number; quarantine_status?: string; text_hash?: string;
  metadata?: Record<string, string>;
}
export interface CreatorContext {
  entities: { id: string; name: string; kind: string }[];
  sources: CreatorSource[];
  narrative_projects: NarrativeProject[];
}
export interface SourceScope { chapter_ids?: string[]; scene_ids?: string[]; branch_ids?: string[]; start_order?: number | null; end_order?: number | null }
export interface SourceAnchor { source_version_id: string; start: number; end: number; quote: string }
export interface ProjectOutline {
  schema_version: 1;
  chapters: { id: string; title: string; summary: string }[];
  scenes: { id: string; chapter_id: string; title: string; brief: string; order: number; dependencies: string[]; sources: SourceAnchor[]; precondition: string; postcondition: string; forbidden: string[]; merge_policy: "preserve_path_state" | "explicit_assignments" | null; merge_effects: { var: string; op: string; value: unknown }[]; requirements?: Record<string, unknown> | null }[];
  branches: { id: string; source_scene: string; target_scene: string | null; label: string; condition: string; effects: { var: string; op: string; value: unknown }[]; ending_text: string }[];
  variables: { id: string; type: "bool" | "int" | "enum"; default: unknown; enum_values: string[] }[];
  entry_scene_id: string;
}
export interface NarrativeProject {
  id: string; name: string; description: string; version: number; project_hash: string;
  outline: ProjectOutline | null;
  scene_bindings: Record<string, { artifact_id: string; artifact_hash: string; task_revision: number; payload: { quest?: StoryQuest; tree?: StoryTree } }>;
}
export interface NarrativeSource {
  id: string; source_id: string; version: number; source_hash: string;
  source: { title: string; format: string; text: string; status: "inspiration" | "draft" | "canon" | "retired"; allowed_uses: string[]; scope: SourceScope; original_locator?: string };
  adaptation: Record<string, unknown>;
}
export interface NarrativeClaim {
  id: string; claim_hash: string;
  claim: { subject: string; predicate: string; value: unknown; anchors: SourceAnchor[]; scope: SourceScope; interpretation: string };
  decision?: { accepted: boolean } | null;
}
export interface NarrativePreview {
  preview_id: string; project_id: string; project_hash: string; preview_hash: string;
  action_seq: number; scene_id: string; state: Record<string, unknown>;
  branch_history: string[]; visited_scenes: string[];
  snapshot_status?: "current" | "historical_requires_rebase" | "fixed_historical";
  version_mode?: "fixed_historical";
  current_project_hash?: string;
  snapshot_migration?: {
    id: string; from_project_hash: string; to_project_hash: string;
    source_ref: { kind: "preview" | "archive"; id: string };
    policy: string; created_at: string; migration_hash: string;
  };
  idempotent_replay?: boolean;
  view: { text?: string; message?: string; status: string; choices: { id: string; label: string; enabled?: boolean; available?: boolean; reason?: string; target_scene?: string | null }[]; state: Record<string, unknown>; action_seq: number; can_advance?: boolean; [key: string]: unknown };
  coverage: { visited_scene_count: number; declared_scene_count: number; scope: string };
}
export interface MachineRequirements {
  schema_version?: 1;
  stage_count?: number; decision_count?: number; decision_option_count?: number;
  distinct_outcomes?: boolean; participant_ids?: string[];
  game_state_variables?: string[];
  business_roles?: { id: string; role: string; entity_id: string; source_refs: string[] }[];
  business_relations?: { id: string; predicate: string; subject_entity_id: string; object_entity_id: string; source_refs: string[] }[];
}
export interface StoryQuest extends EvolutionQuest {
  metadata?: Record<string, unknown>;
  logic?: {
    variables: { id: string; name?: string; type: string; default: unknown }[];
    branches?: { id: string; from_stage: string; condition: string; to_stage: string; outcome: string; effects: { var: string; op: string; value: unknown }[] }[];
  } | null;
}
export interface StoryTree {
  id: string;
  metadata?: Record<string, unknown>;
  root_node?: string;
  nodes: Record<string, {
    id: string; text: string; speaker_id?: string | null; next_node?: string | null;
    choices?: { text: string; next_node?: string | null; condition?: string; effects?: { var: string; op: string; value: unknown }[] }[];
  }>;
}
export interface StoryPlan {
  premise: string; beats: string[]; choices?: string[]; conflict?: string;
  decisions?: { id: string; event_id: string; prompt?: string; options?: unknown[] }[];
}
export interface StoryGate {
  gate_hash: string; node_id: string; stage: string; title: string; reason: string;
  available_outputs?: Record<string, unknown>;
  permission_required?: boolean;
}
export interface RequirementsReport {
  status: string; machine_status: string; can_continue: boolean;
  checks: { key: string; status: string; blocking: boolean; scope: string; reason: string; expected?: unknown; observed?: unknown }[];
  semantic_status?: string; author_satisfaction?: string; limitations?: string[];
}
export interface StoryRunState {
  run: EvolutionRunState["run"];
  graph?: EvolutionGraph;
  graph_hash?: string;
  skill_version_id?: string | null;
  error?: string;
  feedback_source?: EvolutionRunState["feedback_source"];
  result?: {
    status: string;
    outcomes?: Record<string, { status: string; error?: string; outputs?: Record<string, { value: unknown; content_hash: string; type_name?: string }> }>;
    outputs?: { plan?: StoryPlan; quest?: StoryQuest; bound?: { quest?: StoryQuest; tree?: StoryTree }; report?: Record<string, unknown>; [key: string]: unknown };
  } | null;
  collaboration?: {
    policy: CollaborationPolicy; policy_hash: string; pause_requested: boolean;
    pending_gate?: StoryGate | { gate: StoryGate; gate_hash: string; node_id: string; status: string } | null;
    history: Record<string, unknown>[]; can_pause?: boolean; can_resume?: boolean; phase?: string;
    stage_agreements?: { id: string; title: string; policy_hash: string; version: number;
      operations: { operation: string; title: string; allowed: boolean; review_before: boolean }[] }[];
  };
  stage_change?: Record<string, unknown>;
  method_trial_scope_id?: string | null;
  method_trial_scope_hash?: string | null;
  requirements?: { machine: MachineRequirements; original_brief?: string; original_protected?: string[] };
  narrative_project_id?: string | null;
  narrative_scene_id?: string | null;
  narrative_preview_id?: string | null;
  narrative_transition_id?: string | null;
  requirements_report?: RequirementsReport | Record<string, RequirementsReport> | null;
}
export interface ProductionInput {
  selection: "planned" | "matching" | "template";
  variant: "baseline" | "constrained";
  skill_version_id?: string; prior_run_id?: string;
  trial_scope_id?: string; expected_trial_scope_hash?: string;
  plan_guidance?: Record<string, string>;
  machine_requirements: MachineRequirements;
  collaboration_policy: CollaborationPolicy;
  narrative_project_id?: string;
  narrative_scene_id?: string;
  narrative_preview_id?: string;
  narrative_transition_id?: string;
}
export interface CreatorJob {
  job_id: string; status: string; kind: string; error?: string | null;
  result?: Record<string, unknown> | null;
  events: { type?: string; data?: Record<string, unknown> }[];
}
export interface FiniteIntentRecord extends IntentContractRecord {
  contract: IntentContractRecord["contract"] & { formal_rules: Record<string, unknown>[]; target_error_fingerprints?: string[] };
}
export interface AIPatchReview extends IntentReview {
  baseline_artifact_id: string;
  baseline_artifact_hash: string;
  proposal_trace_run_id?: string;
}
export interface PatchSelection {
  artifact_id: string; artifact_hash: string; bundle_path: string;
  before_text: string; label: string;
}

/** Use one captured project throughout a request, including background-job recovery. */
export function storyCreatorClient(project = currentProject()) {
  const base = `/projects/${encodeURIComponent(project)}/creative/v2`;
  const creative = `/projects/${encodeURIComponent(project)}/creative`;
  const encoded = encodeURIComponent;
  return {
    ...evolutionClient(project),
    context: () => apiGet<CreatorContext>(`${base}/creator/context`),
    addSource: (input: { title: string; text: string; allowed_uses: ["drafting"]; purpose: "reference" | "project_material"; canon_status: "unconfirmed" | "author_supplied" }) =>
      apiPost<{ source: CreatorSource }>(`${base}/creator/sources`, input),
    createTask: (input: { brief: string; display_title?: string; display_synopsis?: string; origin_kind?: "author" | "development" | "unspecified"; protected: string[]; participation: CollaborationMode; use_references: boolean; reference_ids: string[]; model?: string }) =>
      apiPost<{ task: StudioTask }>(`${base}/tasks`, input),
    detail: (taskId: string) => apiGet<{ task: StudioTask; runs: StoryRunState[] }>(`${base}/tasks/${encoded(taskId)}`),
    state: (runId: string) => apiGet<StoryRunState>(`${base}/runs/${encoded(runId)}`),
    produce: (taskId: string, input: ProductionInput) =>
      apiPost<{ job_id: string; run_id?: string; status?: string }>(`${base}/tasks/${encoded(taskId)}/production-jobs`, input),
    pause: (runId: string, policyHash: string) =>
      apiPost<unknown>(`${base}/runs/${encoded(runId)}/pause`, { expected_policy_hash: policyHash }),
    resume: (runId: string, input: { expected_gate_hash: string; action: "continue" | "revise" | "cancel"; guidance?: Record<string, string>; collaboration_policy?: CollaborationPolicy; expected_policy_hash?: string; actor_kind?: "author" | "development_operator" }) =>
      apiPost<{ run_id: string; job_id?: string; status?: string }>(`${base}/runs/${encoded(runId)}/resume`, input),
    job: (jobId: string) => apiGet<CreatorJob>(`/jobs/${encoded(jobId)}`),
    applications: (taskId: string) => apiGet<{ applications: IntentApplicationReceipt[] }>(`${base}/tasks/${encoded(taskId)}/edit-applications`),
    application: (applicationId: string) => apiGet<ApplicationDetail>(`${base}/intent-applications/${encoded(applicationId)}`),
    continueEditedPreview: (applicationId: string, previewId: string, seq: number) =>
      apiPost<Record<string, unknown>>(`${base}/intent-applications/${encoded(applicationId)}/continue-preview`, { preview_id: previewId, expected_seq: seq }),
    continueOriginalTask: (previewId: string, seq: number) =>
      apiPost<Record<string, unknown>>(`${base}/previews/${encoded(previewId)}/continue-original`, { expected_seq: seq }),
    readTaskPreview: (previewId: string) => apiGet<Record<string, unknown>>(`${base}/previews/${encoded(previewId)}`),
    stepHistoricalTask: (previewId: string, input: { action_id: string; expected_seq: number; choice_id?: string }) =>
      apiPost<Record<string, unknown>>(`${base}/previews/${encoded(previewId)}/historical-step`, input),
    trialScopes: (taskId: string) => apiGet<{ scopes: MethodTrialScope[] }>(`${base}/tasks/${encoded(taskId)}/trial-scopes`),
    grantTaskTrial: (taskId: string, input: { expected_revision: number; version_ids: string[]; machine_requirements: MachineRequirements; operation_id: string; reason: string; actor_kind: "author" | "development_operator" }) =>
      apiPost<MethodTrialScope>(`${base}/tasks/${encoded(taskId)}/trial-scopes`, input),
    revokeTaskTrial: (scopeId: string, permitHash: string, reason: string) =>
      apiPost<MethodTrialScope>(`${base}/trial-scopes/${encoded(scopeId)}/revoke`, { expected_permit_hash: permitHash, reason }),
    intentHistory: (taskId: string) => apiGet<{ contracts: FiniteIntentRecord[] }>(`${creative}/tasks/${encoded(taskId)}/intent-contracts`),
    finiteIntent: (taskId: string, input: {
      expected_revision: number; baseline_artifact_id: string; baseline_artifact_hash: string;
      allowed_paths: string[]; hard_locks: { path: string }[]; formal_rules: Record<string, unknown>[];
      soft_text: string[]; target_error_fingerprints: string[]; modification_goal: string;
      supersedes_contract_id?: string; expected_parent_hash?: string; change_reason?: string;
    }) => apiPost<FiniteIntentRecord>(`${creative}/tasks/${encoded(taskId)}/intent-contracts`, input),
    proposePatch: (contractId: string, revision: number, contractHash: string) =>
      apiPost<{ job_id: string }>(`${base}/intent-contracts/${encoded(contractId)}/propose-patch`, { expected_revision: revision, expected_contract_hash: contractHash }),
    projects: () => apiGet<{ projects: NarrativeProject[] }>(`${base}/narrative-projects`),
    createProject: (name: string, description: string) => apiPost<{ project: NarrativeProject }>(`${base}/narrative-projects`, { name, description }),
    project: (projectId: string) => apiGet<{ project: NarrativeProject; sources: NarrativeSource[]; claims: NarrativeClaim[] }>(`${base}/narrative-projects/${encoded(projectId)}`),
    saveOutline: (projectId: string, projectHash: string, outline: ProjectOutline) => apiPost<{ project: NarrativeProject }>(`${base}/narrative-projects/${encoded(projectId)}/outline`, { expected_project_hash: projectHash, outline }),
    addProjectSource: (projectId: string, projectHash: string, source: { title: string; format: string; text: string; status: string; allowed_uses: string[]; scope: SourceScope; original_locator?: string }) =>
      apiPost<{ source: NarrativeSource; project: NarrativeProject }>(`${base}/narrative-projects/${encoded(projectId)}/sources`, { expected_project_hash: projectHash, source }),
    addClaim: (projectId: string, projectHash: string, claim: NarrativeClaim["claim"]) =>
      apiPost<{ claim: NarrativeClaim; project: NarrativeProject }>(`${base}/narrative-projects/${encoded(projectId)}/claims`, { expected_project_hash: projectHash, claim }),
    decideClaim: (projectId: string, projectHash: string, claimId: string, accept: boolean, rejectClaimIds: string[]) =>
      apiPost<{ project: NarrativeProject }>(`${base}/narrative-projects/${encoded(projectId)}/claims/${encoded(claimId)}/decision`, { expected_project_hash: projectHash, accept, reject_claim_ids: rejectClaimIds }),
    bindScene: (projectId: string, projectHash: string, sceneId: string, artifactId: string, artifactHash: string) =>
      apiPost<NarrativeProject>(`${base}/narrative-projects/${encoded(projectId)}/scenes/${encoded(sceneId)}/artifact`, { expected_project_hash: projectHash, artifact_id: artifactId, expected_artifact_hash: artifactHash }),
    projectPreview: (projectId: string, projectHash: string) => apiPost<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/previews`, { expected_project_hash: projectHash }),
    projectPreviewState: (projectId: string, previewId: string) => apiGet<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}`),
    rebaseProjectPreview: (projectId: string, previewId: string, projectHash: string, previewHash: string) =>
      apiPost<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}/rebase`, { expected_project_hash: projectHash, expected_preview_hash: previewHash }),
    projectPreviewCompatibility: (projectId: string, previewId: string, projectHash: string, previewHash: string) =>
      apiPost<PreviewCompatibility>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}/compatibility`, { expected_project_hash: projectHash, expected_preview_hash: previewHash }),
    continueOriginalProject: (projectId: string, previewId: string, projectHash: string, previewHash: string) =>
      apiPost<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}/continue-original`, { expected_project_hash: projectHash, expected_preview_hash: previewHash }),
    projectStep: (projectId: string, previewId: string, input: { action_id: string; expected_seq: number; choice_id?: string; transition_id?: string }) =>
      apiPost<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}/step`, input),
    archives: (projectId: string) => apiGet<{ archives: { id: string; name: string; created_at: string; project_hash: string }[] }>(`${base}/narrative-projects/${encoded(projectId)}/archives`),
    archive: (projectId: string, previewId: string, name: string, sequence: number) => apiPost<unknown>(`${base}/narrative-projects/${encoded(projectId)}/previews/${encoded(previewId)}/archives`, { name, expected_seq: sequence }),
    restoreArchive: (projectId: string, projectHash: string, archiveId: string, operationId?: string) => apiPost<NarrativePreview>(`${base}/narrative-projects/${encoded(projectId)}/archives/${encoded(archiveId)}/restore`, { expected_project_hash: projectHash, ...(operationId ? { operation_id: operationId } : {}) }),
  };
}

export const CREATOR_OPERATIONS: Record<string, string> = {
  plan_events: "故事大纲", draft_quest: "场景梗概",
  draft_logic: "选择与后果", logic_contract: "选择与后果",
  draft_dialogue: "角色对白", dialogue_constrained: "角色对白",
  bind_dialogue: "准备试玩", validate_decisions: "检查选择与后果",
  coverage_audit: "检查分支与条件", audit: "检查设定引用与逻辑", package: "整理草稿",
};
export function creatorGraphTitle(graph?: EvolutionGraph): string {
  return graph?.name || "尚未形成制作计划";
}
export type CreatorStatusContext = "execution" | "requirements" | "author" | "comparison" | "trial" | "review" | "preview";
export function creatorStatus(value?: string, context: CreatorStatusContext = "execution"): string {
  const contextual: Partial<Record<CreatorStatusContext, Record<string, string>>> = {
    requirements: { passed: "符合要求", failed: "不符合要求", unknown: "需确认", withheld: "暂无检查结论" },
    author: { accepted: "已采用", rejected: "未采用", unknown: "尚未决定", pending: "待确认" },
    comparison: { unknown: "暂无比较结果", withheld: "暂无可比结果", unassessed: "尚未比较", passed: "本例检查通过", target_resolved: "本例问题已解决", tradeoff: "存在取舍", regressed: "本例结果退步" },
    trial: { allowed: "试用已开启", unlicensed: "试用未开启", unknown: "试用状态未知" },
    review: { ready_for_author: "修改建议已就绪", unknown: "待你确认", withheld: "待你确认", needs_confirmation: "请确认修改范围", passed: "结构检查通过", failed: "检查未通过" },
    preview: { completed: "本段结束", finished: "本条路线结束", unknown: "状态未返回" },
  };
  if (value && contextual[context]?.[value]) return contextual[context]![value];
  const labels: Record<string, string> = {
    queued: "等待开始", running: "生成中", waiting_user: "待确认", paused: "已暂停",
    pending: "尚未开始", ready: "可以继续", succeeded: "已完成本步", completed: "本轮已结束",
    completed_with_skips: "部分完成，仍有未完成项", failed: "执行失败", failed_setup: "配置未就绪",
    cancelled: "已停止", interrupted: "已中断", blocked: "条件未满足", blocked_dependency: "上游尚未完成",
    reused: "复用已有结果", budget_exhausted: "已达到调用上限", unknown: "状态未知",
    passed: "检查通过", withheld: "暂不能证明", waiting_choice: "请选择", waiting_advance: "可继续",
    finished: "本条路线结束", deadlock: "本条路线无法继续", unsupported: "暂不支持此结构",
    waiting_transition: "请选择下一场景或结局",
    accepted: "作者已采用", rejected: "作者未采用", allowed: "已允许试用", unlicensed: "未授权试用",
    schema_validated: "结构已检查", compiled: "可执行", unassessed: "尚未比较", done: "已结束",
    ready_for_author: "修改建议已就绪", needs_confirmation: "请确认修改范围",
    proven: "在声明范围内获证明", refuted: "发现反例",
    target_resolved: "该例目标已解决", tradeoff: "该例存在取舍", regressed: "该例出现退步",
  };
  return value ? labels[value] ?? value : contextual[context]?.unknown ?? "状态未返回";
}
