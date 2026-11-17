import { apiDelete, apiGet, apiPatch, apiPost, currentProject } from "./api";

export type StudioParticipation = "delegate" | "direction" | "edit";
export type StudioFeedbackScope = "content" | "project" | "workflow";

export interface StudioTaskInput {
  brief: string;
  display_title?: string;
  display_synopsis?: string;
  origin_kind?: "author" | "development" | "unspecified";
  protected: string[];
  participation: StudioParticipation;
  use_references: boolean;
  model?: string;
  workflow_id?: string;
}

export interface StudioTask {
  id: string;
  revision: number;
  request: StudioTaskInput;
  created_at?: string;
  base_version?: string;
  display_title?: string;
  display_synopsis?: string;
  origin_kind?: "author" | "development" | "unspecified";
}

export function taskTitle(task?: StudioTask | null): string {
  return task?.display_title?.trim() || "未命名故事";
}

export function taskSynopsis(task?: StudioTask | null): string {
  return task?.display_synopsis?.trim() || "";
}

export function taskOrigin(task: StudioTask): "author" | "development" | "unspecified" {
  return task.origin_kind ?? task.request.origin_kind ?? "unspecified";
}

export interface StudioWorkflowNode {
  id: string;
  operation: string;
  depends_on?: string[];
  optional_dependencies?: string[];
  parameters?: Record<string, unknown>;
  guidance?: string;
}

export interface StudioMethodSlot {
  name: string;
  source: "brief" | "protected" | "entities" | "quests" | "reference_ids";
  description: string;
}

export interface StudioWorkflowSpec {
  name: string;
  description?: string;
  nodes: StudioWorkflowNode[];
  applicability?: string[];
  input_slots?: StudioMethodSlot[];
  method_source_ids?: string[];
}

export interface StudioRun {
  id: string;
  revision: number;
  status: string;
  error?: string;
  cancel_requested?: boolean;
  waiting_question?: string;
  workflow?: StudioWorkflowSpec | null;
  workflow_source?: string;
  learning_job_id?: string | null;
}

export interface StudioNode {
  node_id: string;
  operation: string;
  status: string;
  attempts: number;
  error?: string;
  artifact_id?: string | null;
}

export interface StudioArtifact {
  id: string;
  kind: string;
  payload: Record<string, unknown>;
  revision: number;
  content_hash: string;
  node_id?: string;
  stale?: boolean;
  created_at?: string;
}

export interface StudioDetail {
  task: StudioTask;
  runs: StudioRun[];
  run: StudioRun | null;
  nodes: StudioNode[];
  outcomes?: StudioNode[];
  artifacts: StudioArtifact[];
}

export interface StudioWorkflow {
  id: string;
  name: string;
  description?: string;
  scope: string;
  spec: StudioWorkflowSpec;
  disabled?: boolean;
  content_hash?: string;
  memory_kind?: string;
  technical_status?: string;
  acceptance_status?: "pending" | "accepted" | "rejected";
}

export interface StudioMethodLearning {
  status: string;
  job_id?: string | null;
  workflow?: StudioWorkflow | null;
  outcome?: { status: string; error?: string } | null;
  error?: string;
  attempts?: number;
}

export interface StudioPreviewChoice {
  id: string;
  label: string;
  enabled?: boolean;
  available?: boolean;
  reason?: string;
}

export interface StudioPreview {
  preview_id: string;
  version_mode?: "fixed_historical";
  view: {
    text?: string;
    choices: StudioPreviewChoice[];
    state: Record<string, unknown>;
    status: string;
    action_seq: number;
    can_advance?: boolean;
    message?: string;
    presentation?: "player" | "logic";
    state_names?: Record<string, string>;
    state_changes?: { id: string; name: string; before: unknown; after: unknown }[];
    [key: string]: unknown;
  };
}

/** Keep terminal states separate from missing text; never invent an ending. */
export function previewText(preview: StudioPreview): string {
  const view = preview.view;
  if (view.text?.trim()) return view.text;
  if (view.message?.trim()) return view.message;
  if (["completed", "finished"].includes(view.status)) return "";
  const reasons: Record<string, string> = {
    blocked: "当前条件未满足，无法继续。",
    deadlock: "当前路线没有可继续的选项。",
    unavailable: "当前场景不可用。",
    unsupported: "此草稿的结构暂不支持试玩。",
    dangling: "下一节点不存在，请检查场景连接。",
    cycle: "当前路线遇到循环，请检查场景连接。",
    truncated: "已达到本次试玩的步数上限。",
  };
  return reasons[view.status] ?? "当前节点没有正文。";
}

export interface StudioCapabilities {
  project: string;
  can_run: boolean;
  model_required: boolean;
  preview_quests: { id: string; title: string; has_logic: boolean }[];
  submit_review_available?: boolean;
  reason?: string;
}

export interface StudioPreference { id: string; text: string; disabled: boolean }

function readPreview(raw: Record<string, unknown>): StudioPreview {
  const source = raw.view && typeof raw.view === "object" ? raw.view as Record<string, unknown> : raw;
  if (typeof raw.preview_id !== "string" || !Number.isInteger(source.action_seq) || !Array.isArray(source.choices)) {
    throw new Error("预演响应缺少会话、动作序号或选择列表，请检查服务接口。");
  }
  return { preview_id: raw.preview_id, view: source as StudioPreview["view"] };
}

/** Capture the project once; an outstanding request must never drift to another project. */
export function creativeClient(project = currentProject()) {
  const prefix = `/projects/${encodeURIComponent(project)}/creative`;
  const taskPath = (id: string) => `${prefix}/tasks/${encodeURIComponent(id)}`;
  const runPath = (id: string) => `${prefix}/runs/${encodeURIComponent(id)}`;
  return {
    capabilities: () => apiGet<StudioCapabilities>(`${prefix}/capabilities`),
    listTasks: () => apiGet<{ tasks: StudioTask[] }>(`${prefix}/tasks`),
    createTask: (input: StudioTaskInput) =>
      apiPost<{ task: StudioTask }>(`${prefix}/tasks`, input).then((result) => result.task),
    getTask: (id: string) => apiGet<StudioDetail>(taskPath(id)),
    startRun: (id: string) => apiPost<{ job_id: string; run_id: string; status: string }>(`${taskPath(id)}/runs`, {}),
    cancelRun: (id: string) => apiPost<unknown>(`${runPath(id)}:cancel`, {}),
    continueRun: (id: string) => apiPost<unknown>(`${runPath(id)}:continue`, {}),
    listWorkflows: () => apiGet<{ workflows: StudioWorkflow[] }>(`${prefix}/workflows`),
    setWorkflowDisabled: (id: string, disabled: boolean) =>
      apiPatch<unknown>(`${prefix}/workflows/${encodeURIComponent(id)}`, { disabled }),
    reviseWorkflow: (id: string, input: { expected_hash: string; name: string; spec: StudioWorkflowSpec }) =>
      apiPatch<{ workflow: StudioWorkflow }>(`${prefix}/workflows/${encodeURIComponent(id)}`, input),
    acceptMethod: (id: string, expectedHash: string, status: "accepted" | "rejected") =>
      apiPatch<{ workflow: StudioWorkflow }>(`${prefix}/workflows/${encodeURIComponent(id)}`,
        { expected_hash: expectedHash, acceptance_status: status }),
    learnMethod: (id: string, expectedRevision: number, name: string) =>
      apiPost<{ job_id?: string; status: string }>(`${runPath(id)}:learn-method`,
        { expected_revision: expectedRevision, name }),
    methodLearning: (id: string) => apiGet<StudioMethodLearning>(`${runPath(id)}/method-learning`),
    listPreferences: () => apiGet<{ preferences: StudioPreference[] }>(`${prefix}/preferences`),
    disablePreference: (id: string) => apiDelete<unknown>(`${prefix}/preferences/${encodeURIComponent(id)}`),
    submitReview: (id: string) =>
      apiPost<{ review_item_id: string; status: string }>(`${taskPath(id)}:submit-review`, {}),
    saveWorkflow: (input: { name: string; run_id: string; scope: "project" | "personal" }) =>
      apiPost<{ id?: string }>(`${prefix}/workflows`, input),
    feedback: (id: string, input: { text: string; expected_revision: number; scope: StudioFeedbackScope; target_operation?: string }) =>
      apiPost<{ task: StudioTask; affected_operations: string[] }>(`${taskPath(id)}/feedback`, input),
    reviseArtifact: (id: string, input: { expected_hash: string; payload: Record<string, unknown> }) =>
      apiPost<{ task: StudioTask; artifact: StudioArtifact }>(`${prefix}/artifacts/${encodeURIComponent(id)}/revisions`, input),
    restoreArtifact: (id: string, input: { expected_revision: number; source_hash: string }) =>
      apiPost<{ task: StudioTask; artifact: StudioArtifact; affected_operations: string[] }>(`${prefix}/artifacts/${encodeURIComponent(id)}:restore`, input),
    startPreview: async (id: string, questId?: string, artifactId?: string) =>
      readPreview(await apiPost<Record<string, unknown>>(`${taskPath(id)}/preview`,
        questId ? { quest_id: questId } : artifactId ? { artifact_id: artifactId } : {})),
    stepPreview: (id: string, input: { action_id: string; choice_id?: string; expected_seq: number }) =>
      apiPost<Record<string, unknown>>(`${prefix}/previews/${encodeURIComponent(id)}/steps`, input).then(readPreview),
  };
}

export const STUDIO_OPERATION_LABELS: Record<string, string> = {
  interpret: "理解目标与边界", _interpret: "理解目标与边界", context: "项目资料接地",
  plan: "构建创作流程", _plan: "构建创作流程",
  plan_events: "事件与创意计划", draft_quest: "任务内容", draft_dialogue: "对白正文",
  draft_logic: "任务逻辑", bind_dialogue: "对白条件与效果", audit: "内容与逻辑检查",
  compare: "修订前后比较", package: "整理已有产物",
  learn_method: "从执行过程提炼方法", _learn_method: "从执行过程提炼方法",
};

export const STUDIO_STATUS_LABELS: Record<string, string> = {
  draft: "已保存，尚未运行", queued: "等待执行", pending: "等待依赖", ready: "可以执行",
  running: "执行中", waiting_user: "等待方向确认", succeeded: "已完成", completed: "已完成",
  retrying: "修正技术问题中",
  completed_with_skips: "部分完成", skipped_retry_exhausted: "达到重试上限，已暂缓",
  skipped_nonretryable: "无法重试，已暂缓", blocked_dependency: "缺少必需输入",
  cancelled: "已取消", interrupted: "执行中断", failed_setup: "环境或配置未就绪", failed: "执行失败",
  waiting_choice: "等待你的选择", waiting_advance: "等待推进", finished: "已结束",
  blocked: "条件未满足", unsupported: "暂不支持此语义",
};
