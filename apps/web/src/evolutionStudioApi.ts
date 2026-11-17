import { apiGet, apiPost, currentProject } from "./api";
import type { StudioPreview, StudioTask, StudioTaskInput } from "./studioApi";
import type { ApplicationContinuation } from "./storyCreatorApi";

export interface EvolutionNode {
  id: string;
  operation: string;
  role_version: string;
  guidance?: string;
  product_key?: string;
  problem_key?: string;
  context_bindings?: Record<string, string>;
}
export interface EvolutionGraph {
  id: string;
  version: number;
  name: string;
  nodes: EvolutionNode[];
  data_edges: { source_node: string; source_port: string; target_node: string; target_port: string }[];
  control_edges?: { source_node: string; target_node: string }[];
}
export interface EvolutionEvent {
  seq: number;
  type: string;
  node_id?: string;
  created_at?: string;
  payload: Record<string, unknown>;
}
export interface HarnessRequest {
  id: string;
  node_id: string;
  attempt: number;
  budget_id: string;
  request_hash: string;
  context_hash: string;
  request: Record<string, unknown>;
  context_manifest: Record<string, unknown>;
  reconstruction_scope: string;
}
export interface EvolutionQuest {
  id: string;
  title: string;
  objective: string;
  stages?: { id: string; summary: string }[];
}
export interface EvolutionTree {
  id: string;
  nodes: Record<string, { id: string; text: string; choices?: { text: string; next_node?: string }[] }>;
}
export interface EvolutionResult {
  status: string;
  outputs?: {
    bound?: { quest?: EvolutionQuest; tree?: EvolutionTree };
    report?: Record<string, unknown>;
    [key: string]: unknown;
  };
  outcomes?: Record<string, { status: string; error?: string; input_hash?: string; signature?: string }>;
}
export interface EvolutionArtifact {
  id: string;
  revision: number;
  kind: string;
  content_hash: string;
  payload: { quest?: EvolutionQuest; tree?: EvolutionTree };
  stale: boolean;
}
export interface EvolutionRunState {
  run: { id: string; status: string; revision: number; error?: string; cancel_requested?: boolean };
  graph?: EvolutionGraph;
  result?: EvolutionResult | null;
  error?: string;
  feedback_source?: { artifact_id: string; artifact_hash: string; kind: string } | null;
  graph_hash?: string;
  skill_version_id?: string | null;
  [key: string]: unknown;
}
export interface EvolutionVersion {
  id: string;
  version: number;
  version_hash: string;
  parent_version_id?: string | null;
  graph: EvolutionGraph;
  entries?: Record<string, unknown>[];
  conditions?: Record<string, unknown>;
  delta?: { graph_compilation?: Record<string, unknown>; proposal?: Record<string, unknown> };
  state: {
    technical_status: string;
    trial_status: string;
    author_acceptance: string;
    improvement_status: string;
  };
}
export interface EvolutionFamily {
  id: string;
  name: string;
  description?: string;
  family_hash: string;
  default_version_id?: string | null;
  disabled?: boolean;
  versions: EvolutionVersion[];
}
export interface MethodOutcome {
  status: string;
  candidate_version_id?: string | null;
  evidence?: { error?: string; author_acceptance?: string; [key: string]: unknown };
}
export interface MethodAttempt {
  id: string; attempt: number; status: string; reason: string;
  events?: { status: string; evidence?: Record<string, unknown> }[];
  [key: string]: unknown;
}
export interface MethodTrial {
  trial_key?: string; candidate_version_id: string; execution_attempt_id?: string;
  status: string; run_id?: string; error?: string; comparison?: Record<string, unknown>;
  execution_observation?: { mode: string; model_dispatches?: number; confirmed_provider_responses: number };
  [key: string]: unknown;
}
export interface MethodEpisode {
  id: string; parent_version_id: string; budget_owner: string;
  result?: MethodOutcome | null; original_result?: MethodOutcome | null;
  attempt_state: { status: string; latest?: MethodAttempt | null; attempts: MethodAttempt[] };
  source: { run_id: string; text: string; source_kind: "author_feedback" | "development_input" };
  trials?: MethodTrial[];
}
export interface MethodJob {
  job_id?: string; status?: string;
  result?: { id?: string; result?: MethodOutcome; trace_run_id?: string };
  error?: string | null;
}
export interface IntentContractRecord {
  contract: {
    id: string; version: number; task_id: string; task_revision: number; confirmed: boolean;
    baseline_artifact_id: string; baseline_artifact_hash: string;
    allowed_paths: string[]; hard_locks: { path: string }[];
    modification_goal: string; soft_text: string[];
  };
  contract_hash: string;
  phase: string;
  change_reason: string;
}
export interface IntentDecision {
  decision: "accepted" | "rejected";
  decision_hash: string;
  goal_status: string;
  [key: string]: unknown;
}
export interface IntentReview {
  id: string;
  task_revision: number;
  contract_id: string;
  review_hash: string;
  candidate: { ops: { op: string; path: string; value?: unknown }[] };
  report: {
    status: "blocked" | "needs_confirmation" | "ready_for_author";
    reasons: string[]; goal_status: string; changed_paths: string[];
    resolved_errors: string[]; remaining_targets: string[]; introduced_errors: string[];
    formal_results: Record<string, unknown>[]; soft_results: Record<string, string>[];
    limitations: string[];
    [key: string]: unknown;
  };
  author_decision?: IntentDecision | null;
}
export interface IntentApplicationReceipt {
  id: string;
  revision: number;
  artifact_id: string;
  receipt_hash: string;
  goal_status: string;
  writes_canon: boolean;
  continuation?: ApplicationContinuation | null;
  impact?: Record<string, unknown>;
  source_artifact_id?: string;
  source_artifact_hash?: string;
  artifact_hash?: string;
  [key: string]: unknown;
}
function readPreview(raw: Record<string, unknown>): StudioPreview {
  const view = raw.view && typeof raw.view === "object" ? raw.view as Record<string, unknown> : raw;
  if (typeof raw.preview_id !== "string" || !Number.isInteger(view.action_seq) || !Array.isArray(view.choices)) {
    throw new Error("预演响应缺少会话、动作序号或选择列表，请检查服务接口。");
  }
  return { preview_id: raw.preview_id, view: view as StudioPreview["view"] };
}

/** Capture a project; old in-flight requests cannot move to a different workspace. */
export function evolutionClient(project = currentProject()) {
  const base = `/projects/${encodeURIComponent(project)}/creative/v2`;
  const creative = `/projects/${encodeURIComponent(project)}/creative`;
  const id = encodeURIComponent;
  return {
    tasks: () => apiGet<{ tasks: StudioTask[] }>(`${base}/tasks`),
    task: (taskId: string) => apiGet<{ task: StudioTask; runs: EvolutionRunState[] }>(`${base}/tasks/${id(taskId)}`),
    create: (input: StudioTaskInput) => apiPost<{ task: StudioTask }>(`${base}/tasks`, input),
    graph: (variant: "baseline" | "constrained") => apiGet<{ graph: EvolutionGraph }>(`${base}/graphs/${variant}`),
    start: (taskId: string, input: { variant: "baseline" | "constrained"; selection?: "template" | "matching"; skill_version_id?: string; prior_run_id?: string; plan_guidance?: Record<string, string> }) =>
      apiPost<{ run_id: string; job_id: string }>(`${base}/tasks/${id(taskId)}/runs`, input),
    run: (runId: string) => apiGet<EvolutionRunState>(`${base}/runs/${id(runId)}`),
    events: (runId: string, afterSeq: number) => apiGet<{ events: EvolutionEvent[] }>(`${base}/runs/${id(runId)}/events?after_seq=${afterSeq}`),
    requests: (runId: string) => apiGet<{ requests: HarnessRequest[] }>(`${base}/runs/${id(runId)}/requests`),
    cancel: (runId: string) => apiPost<unknown>(`${base}/runs/${id(runId)}/cancel`, {}),
    preview: (runId: string) => apiPost<Record<string, unknown>>(`${base}/runs/${id(runId)}/preview`, {}).then(readPreview),
    step: (previewId: string, input: { action_id: string; expected_seq: number; choice_id?: string }) =>
      apiPost<Record<string, unknown>>(`${base}/previews/${id(previewId)}/step`, input).then(readPreview),
    feedback: (taskId: string, input: { text: string; expected_revision: number; scope: "content" | "workflow"; target_operation?: string; anchor?: Record<string, unknown> }) =>
      apiPost<{ task: StudioTask; affected_operations?: string[] }>(`${base}/tasks/${id(taskId)}/feedback`, input),
    families: () => apiGet<{ families: EvolutionFamily[] }>(`${base}/families`),
    saveSkill: (runId: string, name: string) => apiPost<{ family?: EvolutionFamily; version?: EvolutionVersion }>(`${base}/runs/${id(runId)}/skills`, { name }),
    allowTrial: (versionId: string, familyHash: string, allowed: boolean, actorKind: "author" | "development_operator" = "author") => apiPost<unknown>(`${base}/versions/${id(versionId)}/trial`, { expected_family_hash: familyHash, allowed, actor_kind: actorKind }),
    decide: (versionId: string, familyHash: string, decision: "accepted" | "rejected", makeDefault: boolean) =>
      apiPost<unknown>(`${base}/versions/${id(versionId)}/decision`, { expected_family_hash: familyHash, decision, make_default: makeDefault }),
    episodes: (familyId: string) => apiGet<{ episodes: MethodEpisode[] }>(`${base}/families/${id(familyId)}/episodes`),
    evolve: (familyId: string, sourceRunId: string, text: string, attemptReason?: string, sourceKind: "author_feedback" | "development_input" = "author_feedback") =>
      apiPost<MethodJob>(`${base}/families/${id(familyId)}/evolve`, { source_run_id: sourceRunId, text, source_kind: sourceKind, ...(attemptReason ? { attempt_reason: attemptReason } : {}) }),
    methodJob: (jobId: string) => apiGet<MethodJob>(`/jobs/${id(jobId)}`),
    rollback: (familyId: string, familyHash: string, versionId: string) =>
      apiPost<unknown>(`${base}/families/${id(familyId)}/rollback`, { expected_family_hash: familyHash, version_id: versionId }),
    evidence: (runId: string) => apiGet<Record<string, unknown>>(`${base}/runs/${id(runId)}/evidence`),
    intentContracts: (taskId: string) => apiGet<{ contracts: IntentContractRecord[] }>(`${creative}/tasks/${id(taskId)}/intent-contracts`),
    proposeIntent: (taskId: string, input: {
      expected_revision: number; baseline_artifact_id: string; baseline_artifact_hash: string;
      allowed_paths: string[]; hard_locks: { path: string }[]; modification_goal: string;
      supersedes_contract_id?: string; expected_parent_hash?: string; change_reason?: string;
    }) => apiPost<IntentContractRecord>(`${creative}/tasks/${id(taskId)}/intent-contracts`, input),
    confirmIntent: (contractId: string, revision: number, contractHash: string, actorKind: "author" | "development_operator" = "author") =>
      apiPost<IntentContractRecord>(`${creative}/intent-contracts/${id(contractId)}:confirm`, { expected_revision: revision, expected_contract_hash: contractHash, actor_kind: actorKind }),
    reviewIntent: (contractId: string, revision: number, contractHash: string, path: string, value: string) =>
      apiPost<IntentReview>(`${creative}/intent-contracts/${id(contractId)}/candidate-reviews`, {
        expected_revision: revision, expected_contract_hash: contractHash,
        candidate: { ops: [{ op: "replace", path, value }] },
      }),
    intentReview: (reviewId: string) => apiGet<IntentReview>(`${creative}/intent-reviews/${id(reviewId)}`),
    decideIntent: (reviewId: string, revision: number, reviewHash: string, decision: "accepted" | "rejected", actorKind: "author" | "development_operator" = "author") =>
      apiPost<IntentDecision>(`${creative}/intent-reviews/${id(reviewId)}/author-decision`, { expected_revision: revision, expected_review_hash: reviewHash, decision, actor_kind: actorKind }),
    applyIntent: (reviewId: string, reviewHash: string, decisionHash: string, continuation?: ApplicationContinuation) =>
      apiPost<IntentApplicationReceipt>(`${base}/intent-reviews/${id(reviewId)}/apply`, { expected_review_hash: reviewHash, expected_decision_hash: decisionHash, ...(continuation ? { continuation } : {}) }),
    artifact: (artifactId: string) => apiGet<{ artifact: EvolutionArtifact }>(`${creative}/artifacts/${id(artifactId)}`),
    taskArtifacts: (taskId: string) => apiGet<{ artifacts: EvolutionArtifact[] }>(`${creative}/tasks/${id(taskId)}`),
    previewArtifact: (taskId: string, artifactId: string) =>
      apiPost<Record<string, unknown>>(`${creative}/tasks/${id(taskId)}/preview`, { artifact_id: artifactId }).then(readPreview),
    stepArtifact: (previewId: string, input: { action_id: string; expected_seq: number; choice_id?: string }) =>
      apiPost<Record<string, unknown>>(`${creative}/previews/${id(previewId)}/steps`, input).then(readPreview),
  };
}
