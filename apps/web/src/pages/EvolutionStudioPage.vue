<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import { currentProject, humanizeError, llmConfig } from "../api";
import PageHead from "../components/PageHead.vue";
import {
  evolutionClient, type EvolutionArtifact, type EvolutionEvent, type EvolutionFamily, type EvolutionGraph,
  type EvolutionRunState, type HarnessRequest, type IntentApplicationReceipt, type IntentContractRecord,
  type IntentDecision, type IntentReview, type MethodEpisode, type MethodTrial,
} from "../evolutionStudioApi";
import { previewText, taskOrigin, taskTitle, type StudioPreview, type StudioTask } from "../studioApi";
import { creatorStatus } from "../storyCreatorApi";

const project = ref(currentProject());
let client = evolutionClient(project.value);
let alive = true;
let serial = 0;
let readSerial = 0;
let planReadSerial = 0;
let timer: ReturnType<typeof setTimeout> | undefined;
let methodTimer: ReturnType<typeof setTimeout> | undefined;
let episodeReadSerial = 0;
const busy = ref(false);
const error = ref("");
const notice = ref("");
const tasks = ref<StudioTask[]>([]);
const taskView = ref<"all" | "author" | "development" | "unspecified">("all");
const visibleTasks = computed(() => taskView.value === "all" ? tasks.value : tasks.value.filter(item => taskOrigin(item) === taskView.value));
const task = ref<StudioTask | null>(null);
const runs = ref<EvolutionRunState[]>([]);
const run = ref<EvolutionRunState | null>(null);
const brief = ref("");
const protections = ref("");
const variant = ref<"baseline" | "constrained">("constrained");
const matchHistory = ref(false);
const events = ref<EvolutionEvent[]>([]);
const requests = ref<HarnessRequest[]>([]);
const selectedRequestId = ref("");
const preview = ref<StudioPreview | null>(null);
const previewSource = ref<"v2" | "artifact">("v2");
const appliedArtifact = ref<EvolutionArtifact | null>(null);
const feedback = ref("");
const feedbackScope = ref<"content" | "workflow">("content");
const feedbackTarget = ref("");
const feedbackAnchor = ref<Record<string, unknown> | null>(null);
const anchorText = ref("");
const proposeMethod = ref(false);
const families = ref<EvolutionFamily[]>([]);
const selectedFamilyId = ref("");
const selectedVersionId = ref("");
const skillName = ref("");
const methodEpisodes = ref<MethodEpisode[]>([]);
const methodText = ref("");
const methodReasons = ref<Record<string, string>>({});
const methodJobId = ref("");
const methodJobStatus = ref("");
const methodRunning = computed(() => ["queued", "running"].includes(methodJobStatus.value));
const templateGraph = ref<EvolutionGraph | null>(null);
const planLoading = ref(false);
const planGuidance = ref<Record<string, string>>({});
const intentContracts = ref<IntentContractRecord[]>([]);
const intentPath = ref("");
const intentLocks = ref<string[]>([]);
const intentGoal = ref("");
const intentChangeReason = ref("");
const candidatePath = ref("");
const candidateValue = ref("");
const intentReview = ref<IntentReview | null>(null);
const intentDecision = ref<IntentDecision | null>(null);
const intentReceipt = ref<IntentApplicationReceipt | null>(null);

const family = computed(() => families.value.find((item) => item.id === selectedFamilyId.value));
const version = computed(() => family.value?.versions.find((item) => item.id === selectedVersionId.value));
const graph = computed<EvolutionGraph | undefined>(() => run.value?.graph);
const planGraph = computed(() => version.value?.graph ?? templateGraph.value);
const changedGuidance = computed(() => Object.fromEntries((planGraph.value?.nodes ?? [])
  .filter((node) => (planGuidance.value[node.id] ?? node.guidance ?? "") !== (node.guidance ?? ""))
  .map((node) => [node.id, planGuidance.value[node.id] ?? ""])));
const affectedPlanNodes = computed(() => {
  const affected = new Set(Object.keys(changedGuidance.value));
  const edges = [...(planGraph.value?.data_edges ?? []), ...(planGraph.value?.control_edges ?? [])];
  let changed = true;
  while (changed) {
    changed = false;
    for (const edge of edges) if (affected.has(edge.source_node) && !affected.has(edge.target_node)) {
      affected.add(edge.target_node); changed = true;
    }
  }
  return (planGraph.value?.nodes ?? []).filter((node) => affected.has(node.id));
});
const activeRun = computed(() => run.value != null && ["queued", "running"].includes(run.value.run.status));
const currentContent = computed(() => task.value != null && (appliedArtifact.value
  ? !appliedArtifact.value.stale && task.value.revision === appliedArtifact.value.revision
  : run.value != null && task.value.revision === run.value.run.revision));
const contentSource = computed(() => appliedArtifact.value ? {
  artifact_id: appliedArtifact.value.id, artifact_hash: appliedArtifact.value.content_hash, kind: appliedArtifact.value.kind,
} : run.value?.feedback_source);
const canAnchor = computed(() => currentContent.value && Boolean(contentSource.value));
const bound = computed(() => appliedArtifact.value?.payload ?? run.value?.result?.outputs?.bound);
const report = computed(() => run.value?.result?.outputs?.report);
const request = computed(() => requests.value.find((item) => item.id === selectedRequestId.value));
const visibleEvents = computed(() => events.value.slice(-120));
const intentFields = computed(() => {
  const fields: { path: string; label: string; text: string }[] = [];
  const quest = bound.value?.quest;
  if (quest) {
    const prefix = `/quests/${pointerToken(quest.id)}`;
    fields.push({ path: `${prefix}/title`, label: "任务标题", text: quest.title },
      { path: `${prefix}/objective`, label: "任务目标", text: quest.objective });
    for (const [index, stage] of (quest.stages ?? []).entries()) {
      fields.push({ path: `${prefix}/stages/${index}/summary`, label: `阶段${index + 1}`, text: stage.summary });
    }
  }
  const tree = bound.value?.tree;
  if (tree) for (const [index, [nodeKey, node]] of Object.entries(tree.nodes).entries()) {
    const prefix = `/dialogue_trees/${pointerToken(tree.id)}/nodes/${pointerToken(nodeKey)}`;
    fields.push({ path: `${prefix}/text`, label: `对白${index + 1}`, text: node.text });
    for (const [choiceIndex, choice] of (node.choices ?? []).entries()) {
      fields.push({ path: `${prefix}/choices/${choiceIndex}/text`, label: `对白${index + 1}的选择${choiceIndex + 1}`, text: choice.text });
    }
  }
  return fields;
});
const intentContract = computed(() => intentContracts.value.at(-1));
const intentContractCurrent = computed(() => canAnchor.value && intentContract.value != null
  && intentContract.value.contract.task_revision === task.value?.revision
  && intentContract.value.contract.baseline_artifact_id === contentSource.value?.artifact_id);
const candidateFields = computed(() => intentFields.value.filter((field) => intentContract.value?.contract.allowed_paths.includes(field.path)));
const candidateOriginal = computed(() => candidateFields.value.find((field) => field.path === candidatePath.value));
const reviewCurrent = computed(() => intentContractCurrent.value && intentReview.value != null
  && intentReview.value.task_revision === task.value?.revision && intentReview.value.contract_id === intentContract.value?.contract.id);
const parents = computed(() => {
  const map: Record<string, string[]> = {};
  for (const edge of graph.value?.data_edges ?? []) {
    (map[edge.target_node] ??= []).push(`${operation(edge.source_node)} · ${edge.source_port} → ${edge.target_port}`);
  }
  return map;
});
const versionChanges = computed(() => {
  const current = version.value;
  if (!current) return [];
  const parent = family.value?.versions.find((item) => item.id === current.parent_version_id);
  if (!parent) return ["首个记录版本；没有父版本可以比较。"];
  const before = new Map(parent.graph.nodes.map((node) => [node.id, node]));
  const after = new Map(current.graph.nodes.map((node) => [node.id, node]));
  const changes: string[] = [];
  for (const [id, node] of after) {
    if (!before.has(id)) changes.push(`新增步骤：${operation(node.operation)}`);
    else {
      const previous = before.get(id)!;
      if (previous.operation !== node.operation) changes.push(`替换步骤：${operation(previous.operation)} → ${operation(node.operation)}`);
      if ((previous.guidance ?? "") !== (node.guidance ?? "")) changes.push(`更新指导：${operation(node.operation)}`);
    }
  }
  for (const [id, node] of before) if (!after.has(id)) changes.push(`移除步骤：${operation(node.operation)}`);
  const edgeKey = (edge: EvolutionGraph["data_edges"][number]) => `${edge.source_node}.${edge.source_port} → ${edge.target_node}.${edge.target_port}`;
  const oldEdges = new Set(parent.graph.data_edges.map(edgeKey));
  const newEdges = new Set(current.graph.data_edges.map(edgeKey));
  for (const edge of newEdges) if (!oldEdges.has(edge)) changes.push(`新增数据交接：${edge}`);
  for (const edge of oldEdges) if (!newEdges.has(edge)) changes.push(`移除数据交接：${edge}`);
  const controlKey = (edge: { source_node: string; target_node: string }) => `${edge.source_node} → ${edge.target_node}`;
  const oldControl = new Set((parent.graph.control_edges ?? []).map(controlKey));
  const newControl = new Set((current.graph.control_edges ?? []).map(controlKey));
  for (const edge of newControl) if (!oldControl.has(edge)) changes.push(`新增执行约束：${edge}`);
  for (const edge of oldControl) if (!newControl.has(edge)) changes.push(`移除执行约束：${edge}`);
  return changes.length ? changes : ["图节点和数据边未改变；请同时查看指导条目和实际证据。"];
});

const labels: Record<string, string> = {
  queued: "等待执行", running: "执行中", completed: "运行结束", completed_with_skips: "部分完成",
  succeeded: "步骤完成", failed: "执行失败", failed_setup: "环境或配置未就绪", blocked: "依赖或条件阻塞",
  blocked_dependency: "缺少必需输入", cancelled: "已取消", interrupted: "已中断", reused: "复用已有产物",
  pending: "待判断", unknown: "状态未知", withheld: "暂无结论", accepted: "已采用", rejected: "未采用",
  validated: "检查通过", compiled: "可编译", checked: "已检查", allowed: "已允许试用", not_evaluated: "未评价改善",
  trial: "仅试用", untested: "尚未试用", eligible: "符合技术准入", budget_exhausted: "达到当前策略额度",
  schema_validated: "结构已校验", passed: "技术检查通过", unlicensed: "尚未允许试用", unassessed: "未判断改善",
  target_resolved: "该例目标已解决", tradeoff: "该例存在取舍", regressed: "该例受损",
  proposed: "草拟许可", confirmed: "作者已确认许可", ready_for_author: "修改建议已就绪",
  needs_confirmation: "请确认修改范围",
  claimed_unknown: "调用结果待对账", completed_failed: "已知失败，可说明原因后继续",
  candidate: "已生成候选版本", done: "请求已结束",
  live_generation: "有已确认模型响应的生成运行",
  stored_ir_recompilation_no_model_dispatch: "原始IR确定性重编，零新模型调用",
  unclassified: "执行来源尚未分类",
};
const operationLabels: Record<string, string> = {
  plan_events: "事件与角色计划", draft_quest: "任务正文", draft_logic: "任务逻辑", draft_dialogue: "对白创作",
  logic_contract: "决策与状态设计", validate_decisions: "决策状态门禁", dialogue_constrained: "按状态合同写对白",
  bind_dialogue: "对白条件与效果绑定", audit: "产物与覆盖检查", package: "整理产物",
  coverage_audit: "决策后果覆盖检查",
};
function label(value: string | undefined): string { return value ? labels[value] ?? value : "未知"; }
function nodeStatus(nodeId: string): string {
  const settled = run.value?.result?.outcomes?.[nodeId]?.status;
  if (settled) return settled;
  const statuses: Record<string, string> = {
    "graph.node.started": "running", "graph.node.completed": "succeeded",
    "graph.node.reused": "reused", "graph.node.blocked": "blocked_dependency",
    "graph.node.failed": "failed", "graph.node.cancelled": "cancelled",
  };
  for (let index = events.value.length - 1; index >= 0; index -= 1) {
    const event = events.value[index];
    if (event.node_id === nodeId && statuses[event.type]) return statuses[event.type];
  }
  return "pending";
}
function operation(value: string): string {
  const node = graph.value?.nodes.find((item) => item.id === value);
  return operationLabels[node?.operation ?? value] ?? value;
}
function json(value: unknown): string { return JSON.stringify(value, null, 2) ?? "未记录"; }
function feedbackOperation(value: string): string {
  return ({ logic_contract: "draft_logic", validate_decisions: "draft_logic", dialogue_constrained: "draft_dialogue", coverage_audit: "audit" } as Record<string, string>)[value] ?? value;
}
function pointerToken(value: string): string { return value.replaceAll("~", "~0").replaceAll("/", "~1"); }
function fieldName(path: string): string { return intentFields.value.find((field) => field.path === path)?.label ?? path; }
function restorePlanGuidance(): void {
  planGuidance.value = Object.fromEntries((planGraph.value?.nodes ?? []).map((node) => [node.id, node.guidance ?? ""]));
}
function anchorFeedback(path: string, text: string, fromPreview = false): void {
  const source = contentSource.value;
  if (!canAnchor.value || !source) return;
  feedbackScope.value = "content"; feedbackTarget.value = "";
  feedbackAnchor.value = {
    artifact_id: source.artifact_id, artifact_hash: source.artifact_hash, path,
    ...(fromPreview && preview.value ? { preview_id: preview.value.preview_id, action_seq: preview.value.view.action_seq } : {}),
  };
  anchorText.value = text;
  const intentPointer = path.startsWith("/quest/") && bound.value?.quest
    ? `/quests/${pointerToken(bound.value.quest.id)}${path.slice(6)}`
    : path.startsWith("/tree/") && bound.value?.tree
      ? `/dialogue_trees/${pointerToken(bound.value.tree.id)}${path.slice(5)}` : "";
  if (intentFields.value.some((field) => field.path === intentPointer)) intentPath.value = intentPointer;
}
function anchorPreview(): void {
  const view = preview.value?.view;
  if (!view || typeof view.cursor !== "string") return;
  if (view.phase === "dialogue") {
    anchorFeedback(`/tree/nodes/${pointerToken(view.cursor)}/text`, view.text ?? "", true);
  } else if (view.phase === "quest") {
    const index = bound.value?.quest?.stages?.findIndex((stage) => stage.id === view.cursor) ?? -1;
    if (index >= 0) anchorFeedback(`/quest/stages/${index}/summary`, view.text ?? "", true);
  }
}
function eventTitle(event: EvolutionEvent): string {
  const names: Record<string, string> = {
    "model/request": "开始模型派发", "model/result": "模型返回", "tool/call": "调用领域工具",
    "tool/result": "工具结算", "node/start": "步骤开始", "node/completed": "步骤完成",
    "node/failed": "步骤失败", "graph/start": "执行固定工作图", "graph/completed": "工作图结束",
    "node/reused": "复用未改变产物", "tool/protocol_error": "工具协议问题",
    "graph.started": "执行固定工作图", "graph.completed": "工作图结束",
    "graph.node.started": "步骤开始", "graph.node.completed": "步骤完成",
    "graph.node.reused": "复用未改变产物", "graph.node.failed": "步骤失败",
    "graph.node.blocked": "上游未完成，暂停此步", "graph.node.cancelled": "步骤已取消",
  };
  return names[event.type] ?? event.type;
}
function eventSummary(event: EvolutionEvent): string {
  const payload = event.payload;
  const errorText = payload.error;
  if (typeof errorText === "string") return errorText;
  const status = payload.status;
  const name = payload.name;
  const attempt = payload.attempt;
  return [typeof status === "string" ? label(status) : "", typeof name === "string" ? name : "",
    typeof attempt === "number" ? `第${attempt}次派发` : ""].filter(Boolean).join(" · ");
}
function stopPoll(): void { if (timer) clearTimeout(timer); timer = undefined; }
function reset(): void {
  serial += 1; stopPoll(); task.value = null; run.value = null; runs.value = [];
  events.value = []; requests.value = []; selectedRequestId.value = ""; preview.value = null;
  feedback.value = ""; feedbackAnchor.value = null; anchorText.value = ""; notice.value = ""; error.value = "";
  appliedArtifact.value = null; previewSource.value = "v2";
  intentContracts.value = []; intentReview.value = null; intentDecision.value = null; intentReceipt.value = null;
  intentGoal.value = ""; intentChangeReason.value = ""; intentLocks.value = []; intentPath.value = "";
  candidatePath.value = ""; candidateValue.value = "";
}
async function loadPlan(): Promise<void> {
  const reading = ++planReadSerial;
  const selected = variant.value;
  const api = client;
  planLoading.value = true; templateGraph.value = null;
  try {
    const result = await api.graph(selected);
    if (alive && reading === planReadSerial && selected === variant.value) templateGraph.value = result.graph;
  } finally { if (reading === planReadSerial) planLoading.value = false; }
}
async function lists(): Promise<void> {
  const stamp = serial;
  const [taskList, familyList] = await Promise.all([client.tasks(), client.families()]);
  if (!alive || stamp !== serial) return;
  tasks.value = taskList.tasks;
  families.value = familyList.families;
  if (selectedFamilyId.value && !family.value) { selectedFamilyId.value = ""; selectedVersionId.value = ""; }
  await loadMethodEpisodes();
}

async function loadMethodEpisodes(): Promise<void> {
  const selected = selectedFamilyId.value;
  const reading = ++episodeReadSerial;
  const selectedProject = project.value;
  if (!selected) { methodEpisodes.value = []; return; }
  const result = await client.episodes(selected);
  if (alive && reading === episodeReadSerial && selected === selectedFamilyId.value && selectedProject === project.value) {
    methodEpisodes.value = result.episodes;
  }
}
function stopMethodPoll(): void { if (methodTimer) clearTimeout(methodTimer); methodTimer = undefined; }
async function pollMethodJob(): Promise<void> {
  const jobId = methodJobId.value;
  if (!jobId) return;
  const selectedProject = project.value;
  const job = await client.methodJob(jobId);
  if (!alive || selectedProject !== project.value || jobId !== methodJobId.value) return;
  methodJobStatus.value = job.status ?? "unknown";
  stopMethodPoll();
  if (methodRunning.value) methodTimer = setTimeout(() => { void pollMethodJob().catch((failure) => {
    if (alive && selectedProject === project.value) error.value = `方法进度读取已停止：${humanizeError(failure)}。可手动刷新，不会重新派发模型。`;
  }); }, 4000);
  else {
    await lists();
    if (job.error) error.value = humanizeError(job.error);
    else notice.value = "流程分析已结束，可查看结果和候选版本。";
  }
}
async function proposeWorkflow(episode?: MethodEpisode): Promise<void> {
  await perform(async () => {
    const selected = family.value;
    if (!selected || methodRunning.value) return;
    const sourceRun = episode?.source.run_id ?? run.value?.run.id;
    const text = episode?.source.text ?? methodText.value.trim();
    const reason = episode ? (methodReasons.value[episode.id] ?? "").trim() : undefined;
    if (!sourceRun || !text) throw new Error("请先选择实际运行，并说明要改进的工作方法。");
    if (episode && (!reason || episode.attempt_state.status === "claimed_unknown")) throw new Error("已知失败需说明新诊断；未知调用须先完成对账。");
    const selectedProject = project.value;
    const job = await client.evolve(selected.id, sourceRun, text, reason, episode?.source.source_kind);
    if (!alive || selectedProject !== project.value) return;
    methodJobId.value = job.job_id ?? "";
    methodJobStatus.value = job.status ?? "unknown";
    notice.value = "已提交流程分析。";
    if (methodJobId.value) await pollMethodJob();
    else await loadMethodEpisodes();
  });
}
async function viewMethodTrial(trial: MethodTrial): Promise<void> {
  await perform(async () => {
    if (!trial.run_id) return;
    const state = await client.run(trial.run_id);
    const taskId = typeof state.task_id === "string" ? state.task_id : task.value?.id;
    if (!taskId) throw new Error("试用运行缺少任务绑定。");
    await chooseTask(taskId);
    run.value = state; events.value = []; requests.value = []; preview.value = null;
    await refreshRun();
  });
}
async function perform(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  const actionProject = project.value;
  busy.value = true; error.value = ""; notice.value = "";
  try { await action(); } catch (failure) { if (alive && actionProject === project.value) error.value = humanizeError(failure); }
  finally { busy.value = false; }
}
async function refreshRun(): Promise<void> {
  const runId = run.value?.run.id;
  if (!runId) return;
  const stamp = serial;
  const reading = ++readSerial;
  const api = client;
  const [state, requestList] = await Promise.all([api.run(runId), api.requests(runId)]);
  if (!alive || stamp !== serial || reading !== readSerial || run.value?.run.id !== runId) return;
  run.value = state; requests.value = requestList.requests;
  let cursor = events.value.at(-1)?.seq ?? 0;
  for (let page = 0; page < 5; page += 1) {
    const batch = (await api.events(runId, cursor)).events;
    if (!alive || stamp !== serial || reading !== readSerial || run.value?.run.id !== runId) return;
    if (!batch.length) break;
    const seen = new Set(events.value.map((event) => event.seq));
    events.value = [...events.value, ...batch.filter((event) => !seen.has(event.seq))].sort((a, b) => a.seq - b.seq);
    const next = batch.at(-1)?.seq ?? cursor;
    if (next <= cursor) break;
    cursor = next;
    if (batch.length < 200) break;
  }
  if (!selectedRequestId.value && requests.value.length) selectedRequestId.value = requests.value.at(-1)?.id ?? "";
  stopPoll();
  if (activeRun.value) timer = setTimeout(() => { void refreshRun().catch((failure) => {
    if (alive && stamp === serial) { error.value = `自动读取已停止：${humanizeError(failure)}。可手动刷新，现有结果已保留。`; stopPoll(); }
  }); }, 6000);
}
async function chooseTask(taskId: string): Promise<void> {
  reset();
  const stamp = serial;
  const [detail, contracts, artifacts] = await Promise.all([client.task(taskId), client.intentContracts(taskId), client.taskArtifacts(taskId)]);
  if (!alive || stamp !== serial) return;
  task.value = detail.task; runs.value = detail.runs;
  intentContracts.value = contracts.contracts;
  appliedArtifact.value = artifacts.artifacts.filter((artifact) => artifact.kind === "bound_dialogue"
    && artifact.revision === detail.task.revision && !artifact.stale).at(-1) ?? null;
  brief.value = detail.task.request.brief;
  protections.value = (detail.task.request.protected ?? []).join("\n");
  run.value = detail.runs[0] ?? null;
  if (run.value) await refreshRun();
}
async function begin(): Promise<void> {
  const current = task.value;
  if (!current) return;
  if ((!version.value && planLoading.value) || !planGraph.value) throw new Error("制作计划尚未读取成功，请先读取计划。");
  const stamp = serial;
  const guidance = changedGuidance.value;
  const start = await client.start(current.id, {
    variant: variant.value,
    selection: matchHistory.value && !Object.keys(guidance).length ? "matching" : "template",
    ...(version.value ? { skill_version_id: version.value.id } : {}),
    ...(run.value ? { prior_run_id: run.value.run.id } : {}),
    ...(Object.keys(guidance).length ? { plan_guidance: guidance } : {}),
  });
  if (!alive || stamp !== serial) return;
  serial += 1; stopPoll(); events.value = []; requests.value = []; selectedRequestId.value = ""; preview.value = null;
  feedbackAnchor.value = null; anchorText.value = "";
  appliedArtifact.value = null; intentReview.value = null; intentDecision.value = null; intentReceipt.value = null;
  run.value = { run: { id: start.run_id, status: "queued", revision: current.revision } };
  await refreshRun();
}
async function createAndStart(): Promise<void> {
  await perform(async () => {
    if (!brief.value.trim()) throw new Error("请先写下灵感或制作目的。");
    const actionProject = project.value;
    const model = llmConfig().model;
    const created = await client.create({
      brief: brief.value.trim(), origin_kind: "author", protected: protections.value.split(/\r?\n/).map((row) => row.trim()).filter(Boolean),
      participation: "delegate", use_references: false, ...(model ? { model } : {}),
    });
    if (!alive || actionProject !== project.value) return;
    await chooseTask(created.task.id); await lists(); await begin();
  });
}
async function saveFeedback(): Promise<void> {
  await perform(async () => {
    if (!task.value || !feedback.value.trim()) return;
    const sourceRun = run.value?.run.id;
    const stamp = serial;
    const api = client;
    const methodFamily = family.value;
    const text = feedback.value.trim();
    const response = await api.feedback(task.value.id, {
      text, expected_revision: task.value.revision, scope: feedbackScope.value,
      ...(feedbackAnchor.value && feedbackScope.value === "content" ? { anchor: feedbackAnchor.value }
        : feedbackTarget.value ? { target_operation: feedbackTarget.value } : {}),
    });
    if (!alive || stamp !== serial) return;
    task.value = response.task;
    notice.value = "本次反馈已保存为新任务版本。旧作品保留；开始修订后才会调用模型。";
    preview.value = null;
    feedbackAnchor.value = null; anchorText.value = "";
    if (proposeMethod.value && methodFamily && sourceRun) {
      try {
        const methodJob = await api.evolve(methodFamily.id, sourceRun, text);
        if (!alive || stamp !== serial) return;
        methodJobId.value = methodJob.job_id ?? "";
        methodJobStatus.value = methodJob.status ?? "unknown";
        if (methodJobId.value) await pollMethodJob();
        notice.value += " 已另行提交方法候选提议，尚未接受或设为默认。";
      } catch (failure) {
        notice.value += " 当前反馈已保留。";
        throw new Error(`方法提议未提交：${humanizeError(failure)}`);
      }
    }
    feedback.value = ""; await lists();
  });
}
async function step(choiceId?: string): Promise<void> {
  await perform(async () => {
    if (!preview.value) return;
    const stamp = serial;
    const advance = previewSource.value === "artifact" ? client.stepArtifact : client.step;
    const next = await advance(preview.value.preview_id, {
      action_id: crypto.randomUUID(), expected_seq: preview.value.view.action_seq,
      ...(choiceId ? { choice_id: choiceId } : {}),
    });
    if (alive && stamp === serial) preview.value = next;
  });
}
async function startPreview(): Promise<void> {
  await perform(async () => {
    if (!task.value || !run.value || !currentContent.value) return;
    const stamp = serial;
    if (appliedArtifact.value) {
      const result = await client.previewArtifact(task.value.id, appliedArtifact.value.id);
      if (!alive || stamp !== serial) return;
      preview.value = result;
      previewSource.value = "artifact";
    } else {
      const result = await client.preview(run.value.run.id);
      if (!alive || stamp !== serial) return;
      preview.value = result; previewSource.value = "v2";
    }
  });
}
async function proposeIntent(): Promise<void> {
  await perform(async () => {
    const source = contentSource.value;
    const current = task.value;
    if (!current || !source || !canAnchor.value || !intentPath.value || !intentGoal.value.trim()) return;
    const previous = intentContract.value;
    if (previous && !intentChangeReason.value.trim()) throw new Error("请说明为什么要更新此前的修改许可。");
    const stamp = serial;
    const response = await client.proposeIntent(current.id, {
      expected_revision: current.revision, baseline_artifact_id: source.artifact_id,
      baseline_artifact_hash: source.artifact_hash, allowed_paths: [intentPath.value],
      hard_locks: intentLocks.value.filter((path) => path !== intentPath.value).map((path) => ({ path })),
      modification_goal: intentGoal.value.trim(),
      ...(previous ? { supersedes_contract_id: previous.contract.id, expected_parent_hash: previous.contract_hash,
        change_reason: intentChangeReason.value.trim() } : {}),
    });
    if (!alive || stamp !== serial) return;
    intentContracts.value = [...intentContracts.value, response]; intentReview.value = null;
    intentDecision.value = null; intentReceipt.value = null;
    notice.value = "已保存修改许可草稿。请核对后单独确认，尚未修改作品。";
  });
}
async function confirmIntent(): Promise<void> {
  await perform(async () => {
    const contract = intentContract.value;
    if (!task.value || !contract || !intentContractCurrent.value) return;
    const stamp = serial;
    const response = await client.confirmIntent(contract.contract.id, task.value.revision, contract.contract_hash);
    if (!alive || stamp !== serial) return;
    intentContracts.value = [...intentContracts.value, response];
    notice.value = "已记录作者确认的许可新版本；候选仍须独立审查和选择。";
  });
}
async function reviewIntentCandidate(): Promise<void> {
  await perform(async () => {
    const contract = intentContract.value;
    if (!task.value || !contract?.contract.confirmed || !intentContractCurrent.value || !candidateOriginal.value) return;
    const stamp = serial;
    const response = await client.reviewIntent(contract.contract.id, task.value.revision, contract.contract_hash, candidatePath.value, candidateValue.value);
    if (!alive || stamp !== serial) return;
    intentReview.value = response; intentDecision.value = response.author_decision ?? null; intentReceipt.value = null;
    notice.value = "候选已在隔离副本审查一次，原草稿保持不变。请查看硬检查与未知项后作作者决定。";
  });
}
async function decideIntent(decision: "accepted" | "rejected"): Promise<void> {
  await perform(async () => {
    const review = intentReview.value;
    if (!task.value || !review || !reviewCurrent.value) return;
    const stamp = serial;
    const response = await client.decideIntent(review.id, task.value.revision, review.review_hash, decision);
    if (!alive || stamp !== serial) return;
    intentDecision.value = response;
    notice.value = decision === "accepted" ? "已记录作者选择；点击应用才会生成新版草稿。" : "已记录拒绝；原草稿保持不变。";
  });
}
async function applyIntent(): Promise<void> {
  await perform(async () => {
    const review = intentReview.value;
    const decision = intentDecision.value;
    const current = task.value;
    if (!current || !review || decision?.decision !== "accepted" || !reviewCurrent.value) return;
    const stamp = serial;
    const receipt = await client.applyIntent(review.id, review.review_hash, decision.decision_hash);
    if (!alive || stamp !== serial) return;
    intentReceipt.value = receipt;
    notice.value = `应用已保存为草稿版本${receipt.revision}；正在读取实际新草稿。`;
    preview.value = null; feedbackAnchor.value = null; anchorText.value = "";
    const [artifact, detail] = await Promise.all([client.artifact(receipt.artifact_id), client.task(current.id)]);
    if (!alive || stamp !== serial) return;
    appliedArtifact.value = artifact.artifact; task.value = detail.task; runs.value = detail.runs;
    notice.value = `已载入草稿 v${receipt.revision}。`;
    await lists();
  });
}
async function downloadEvidence(): Promise<void> {
  await perform(async () => {
    if (!run.value) return;
    const data = await client.evidence(run.value.run.id);
    const url = URL.createObjectURL(new Blob([json(data)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `owc-evidence-${run.value.run.id}.json`; link.click();
    URL.revokeObjectURL(url); notice.value = "已导出当前运行的实际记录；技术轨迹不等于研究样本。";
  });
}
function familyChanged(): void {
  selectedVersionId.value = family.value?.default_version_id ?? family.value?.versions.at(-1)?.id ?? "";
  void loadMethodEpisodes().catch((failure) => { error.value = humanizeError(failure); });
}
function storageChanged(event: StorageEvent): void {
  if (event.key !== "owcopilot_project") return;
  reset(); project.value = currentProject(); client = evolutionClient(project.value);
  stopMethodPoll(); episodeReadSerial += 1; methodEpisodes.value = []; methodJobId.value = ""; methodJobStatus.value = "";
  tasks.value = []; families.value = []; selectedFamilyId.value = ""; selectedVersionId.value = "";
  void Promise.all([lists(), loadPlan()]).catch((failure) => { error.value = humanizeError(failure); });
}
watch(variant, () => { void loadPlan().catch((failure) => { error.value = humanizeError(failure); }); });
watch(() => version.value ? `${version.value.id}:${version.value.version_hash}` : JSON.stringify(templateGraph.value), () => {
  planGuidance.value = Object.fromEntries((planGraph.value?.nodes ?? []).map((node) => [node.id, node.guidance ?? ""]));
});
watch(() => Object.keys(changedGuidance.value).length, (count) => { if (count) matchHistory.value = false; });
watch(() => contentSource.value?.artifact_id, () => {
  intentPath.value = intentFields.value.find((field) => field.path.endsWith("/objective"))?.path ?? intentFields.value[0]?.path ?? "";
  intentLocks.value = [];
});
watch(intentPath, (path) => { intentLocks.value = intentLocks.value.filter((lock) => lock !== path); });
watch(() => `${intentContract.value?.contract.id ?? ""}:${contentSource.value?.artifact_id ?? ""}`, () => {
  candidatePath.value = candidateFields.value[0]?.path ?? "";
  candidateValue.value = candidateFields.value[0]?.text ?? "";
});
watch(candidatePath, () => { candidateValue.value = candidateOriginal.value?.text ?? ""; });
onMounted(() => {
  window.addEventListener("storage", storageChanged);
  void Promise.all([lists(), loadPlan()]).catch((failure) => { error.value = humanizeError(failure); });
});
onUnmounted(() => { alive = false; serial += 1; planReadSerial += 1; episodeReadSerial += 1; stopPoll(); stopMethodPoll(); window.removeEventListener("storage", storageChanged); });
</script>

<template>
  <div class="evolution-studio">
    <PageHead overline="WORKFLOW STUDIO" title="流程调试台" purpose="查看生成步骤、工具调用和流程版本。" />
    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>
    <div class="toolbar">
      <span>项目：{{ project }}</span>
      <label>来源<select v-model="taskView"><option value="all">全部记录</option><option value="author">我的故事</option><option value="development">开发记录</option><option value="unspecified">未标记来源</option></select></label><label>已有任务 <select :value="task?.id ?? ''" :disabled="busy" @change="perform(() => chooseTask(($event.target as HTMLSelectElement).value))"><option value="" disabled>选择任务</option><option v-for="item in visibleTasks" :key="item.id" :value="item.id">{{ taskTitle(item) }} · v{{ item.revision }}</option></select></label>
      <button :disabled="busy" @click="perform(lists)">刷新任务与流程</button>
      <button v-if="run" :disabled="busy" @click="perform(refreshRun)">读取当前运行</button>
    </div>
    <section class="panel brief-panel">
      <h2>创作要求</h2>
      <label>创作要求（必填）<textarea v-model="brief" rows="3" /></label>
      <label>必须保留的内容（选填，每行一项）<textarea v-model="protections" rows="2" /></label>
      <div class="actions">
        <label>制作方式 <select v-model="variant"><option value="constrained">先核对决策状态，再写对白</option><option value="baseline">正文与对白先行</option></select></label>
        <button class="primary" :disabled="busy || activeRun || !brief.trim() || !planGraph || (planLoading && !version)" @click="createAndStart">新建并运行</button>
        <button v-if="task" :disabled="busy || activeRun || !planGraph || (planLoading && !version)" @click="perform(begin)">按当前任务版本开始修订</button>
        <button v-if="run && activeRun" :disabled="busy" @click="perform(async () => { await client.cancel(run!.run.id); await refreshRun(); })">取消后续工作</button>
      </div>
      <p class="muted">默认连续委派；下方可随时查看计划和反馈。改动这两个输入框只用于新任务，已有任务通过局部反馈修订。</p>
      <p v-if="task" class="muted">当前任务版本 {{ task.revision }} <span v-if="run"> · 运行输入版本 {{ run.run.revision }} · {{ label(run.run.status) }}</span></p>
    </section>

    <section class="panel shared-plan">
      <div class="section-head"><h2>下一次委派计划</h2><button :disabled="busy || planLoading" @click="perform(loadPlan)">读取基础计划</button></div>
      <p class="muted">修改步骤指导后，下次运行生效。</p>
      <label class="check"><input v-model="matchHistory" type="checkbox" :disabled="busy || Boolean(Object.keys(changedGuidance).length)">按需求匹配已接受的历史方法</label>
      <p v-if="matchHistory" class="muted">优先使用手选版本；否则匹配已采用流程，无匹配时使用下方基础模板。实际使用的流程见运行记录。</p>
      <p v-if="!planGraph" class="empty">{{ planLoading ? '正在读取服务端计划。' : '计划尚未读取成功，请重新读取后再委派。' }}</p>
      <template v-else>
        <p class="muted">{{ version ? `手选技能 v${version.version}` : '基础制作图' }} · {{ planGraph.name }} · 图版本{{ planGraph.version }}</p>
        <div class="plan-editors"><details v-for="node in planGraph.nodes" :key="node.id"><summary>{{ operation(node.operation) }} <span v-if="Object.hasOwn(changedGuidance, node.id)" class="badge">已修改</span></summary><label>这一步如何工作<textarea v-model="planGuidance[node.id]" :disabled="busy" rows="3" maxlength="8000" /></label></details></div>
        <template v-if="Object.keys(changedGuidance).length"><p class="muted">直接修改{{ Object.keys(changedGuidance).length }}步。会检查这些步骤及后续依赖是否需要重算：{{ affectedPlanNodes.map((node) => operation(node.operation)).join('、') }}。复用仍由实际输入和签名决定，不补发已经用尽的额度。编辑计划时不自动换成其他匹配图。</p><button :disabled="busy" @click="restorePlanGuidance">撤回尚未提交的计划编辑</button></template>
      </template>
    </section>

    <div class="work-grid">
      <main class="panel product">
        <div class="section-head"><h2>作品与预演</h2><button v-if="run" :disabled="busy || activeRun || !bound || !currentContent" @click="startPreview">开始预演</button></div>
        <p v-if="appliedArtifact" class="muted">当前展示从存储读取的草稿版本{{ appliedArtifact.revision }}。<span v-if="appliedArtifact.revision !== run?.run.revision">右侧运行图和下方过程仍对应原运行输入版本{{ run?.run.revision }}，不是新草稿的重跑结果。</span></p>
        <p v-if="!run" class="empty">给出灵感后，实际生成的正文和执行结果会出现在这里。</p>
        <p v-else-if="!bound" class="empty">暂无可试玩的草稿。生成进度见右侧步骤。</p>
        <article v-if="bound?.quest" class="quest-copy"><h3>{{ bound.quest.title }}</h3><p>{{ bound.quest.objective }}</p><button v-if="canAnchor" class="text-action" @click="anchorFeedback('/quest/objective', bound.quest.objective)">针对任务目标反馈</button><div v-for="(stage, index) in bound.quest.stages" :key="stage.id"><p>{{ stage.summary }}</p><button v-if="canAnchor" class="text-action" @click="anchorFeedback(`/quest/stages/${index}/summary`, stage.summary)">针对本阶段反馈</button></div></article>
        <details v-if="bound?.tree" class="dialogue"><summary>阅读对白正文</summary><article v-for="(node, nodeKey) in bound.tree.nodes" :key="nodeKey"><p>{{ node.text }}</p><button v-if="canAnchor" class="text-action" @click="anchorFeedback(`/tree/nodes/${pointerToken(nodeKey)}/text`, node.text)">针对这句反馈</button><ul v-if="node.choices?.length"><li v-for="(choice, index) in node.choices" :key="index">{{ choice.text }} <button v-if="canAnchor" class="text-action" @click="anchorFeedback(`/tree/nodes/${pointerToken(nodeKey)}/choices/${index}/text`, choice.text)">针对这个选择反馈</button></li></ul></article></details>
        <section v-if="preview" class="preview">
          <span class="badge">{{ creatorStatus(preview.view.status, "preview") }}</span><p v-if="previewText(preview)" class="scene">{{ previewText(preview) }}</p>
          <p v-if="preview.view.text && preview.view.message && preview.view.message !== preview.view.text" class="muted">{{ preview.view.message }}</p>
          <button v-if="canAnchor && preview.view.text" class="text-action" @click="anchorPreview">定位当前预演位置并反馈</button>
          <div class="choices"><button v-for="choice in preview.view.choices" :key="choice.id" :disabled="busy || choice.enabled === false || choice.available === false" @click="step(choice.id)">{{ choice.label }}<small v-if="choice.reason">{{ choice.reason }}</small></button><button v-if="preview.view.can_advance && !preview.view.choices.length" :disabled="busy" @click="step()">继续</button></div>
          <details><summary>实际状态与经过的路径</summary><pre>{{ json({ state: preview.view.state, history: preview.view.history }) }}</pre></details>
        </section>
        <details v-if="report"><summary>该次运行的技术检查与未能证明的事项</summary><pre>{{ json(report) }}</pre></details>
        <p v-if="run?.error || run?.run.error" class="message error">{{ run.error || run.run.error }}</p>
        <section v-if="task" class="feedback">
          <h3>修改意见</h3><label>修改内容（必填）<textarea v-model="feedback" rows="3" /></label>
          <p v-if="feedbackAnchor && feedbackScope === 'content'" class="anchor-quote">已定位原文：“{{ anchorText }}” <button class="text-action" @click="feedbackAnchor = null; anchorText = ''">取消精确定位</button></p>
          <div class="actions"><label>本次作用 <select v-model="feedbackScope"><option value="content">修改当前作品</option><option value="workflow">重新规划当前做法</option></select></label><label>相关步骤 <select v-model="feedbackTarget" :disabled="Boolean(feedbackAnchor) && feedbackScope === 'content'"><option value="">按本次范围定位</option><option v-for="node in graph?.nodes ?? []" :key="node.id" :value="feedbackOperation(node.operation)">{{ operation(node.operation) }}</option></select></label></div>
          <label class="check"><input v-model="proposeMethod" type="checkbox" :disabled="!family || !run">同时为选中的技能提出长期方法候选</label>
          <p v-if="proposeMethod" class="muted">此建议将另存为所选流程的候选版本。</p>
          <button class="primary" :disabled="busy || activeRun || !feedback.trim()" @click="saveFeedback">保存反馈</button>
        </section>
        <section v-if="task" class="intent-panel">
          <h3>在意图约束下修复当前草稿</h3>
          <p id="evolution-intent-scope" class="muted">替换选中的完整文本字段，检查后另行确认应用。</p>
          <p v-if="!canAnchor" class="empty">需要当前任务版本的实际绑定草稿；旧版本仅供回看。</p>
          <details v-if="canAnchor" :open="!intentContractCurrent">
            <summary>1 · 草拟或更新修改许可</summary>
            <label>允许改动的原文位置<select v-model="intentPath" :disabled="busy" aria-describedby="evolution-intent-scope"><option v-for="field in intentFields" :key="field.path" :value="field.path">{{ field.label }} · {{ field.text.slice(0, 32) }}</option></select></label>
            <p class="anchor-quote">{{ intentFields.find((field) => field.path === intentPath)?.text }}</p>
            <label>修改目标（必填）<textarea v-model="intentGoal" :disabled="busy" rows="2" maxlength="8000" /></label>
            <details><summary>额外明确锁定哪些原文</summary><div class="lock-fields"><label v-for="field in intentFields.filter((field) => field.path !== intentPath)" :key="field.path" class="check"><input v-model="intentLocks" type="checkbox" :value="field.path" :disabled="busy">{{ field.label }}：{{ field.text.slice(0, 48) }}</label></div><p class="muted">本面板只允许选中的一个字段发生实际差异；额外字段锁会写入许可并单独检查。</p></details>
            <label v-if="intentContract">更新许可的原因（必填）<input v-model="intentChangeReason" :disabled="busy" maxlength="4000"></label>
            <button :disabled="busy || activeRun || !intentPath || !intentGoal.trim() || (Boolean(intentContract) && !intentChangeReason.trim())" @click="proposeIntent">保存许可草稿</button>
          </details>
          <template v-if="intentContract">
            <div class="intent-contract"><strong>2 · 修改许可 v{{ intentContract.contract.version }}</strong> <span class="badge">{{ label(intentContract.phase) }}</span><p>{{ intentContract.contract.modification_goal }}</p><p class="muted">允许：{{ intentContract.contract.allowed_paths.map(fieldName).join('、') }}；硬锁{{ intentContract.contract.hard_locks.length }}项。绑定任务版本{{ intentContract.contract.task_revision }}。</p><p v-if="!intentContractCurrent" class="muted">该许可未绑定当前展示草稿，需以新版本重新提出。</p><details><summary>核对许可原文、范围和来源</summary><pre>{{ json(intentContract) }}</pre></details><button :disabled="busy || activeRun || !intentContractCurrent || intentContract.contract.confirmed" @click="confirmIntent">我确认这份修改许可</button></div>
            <div v-if="intentContract.contract.confirmed && intentContractCurrent" class="candidate-edit"><h4>3 · 提出隔离候选</h4><label>许可内的替换位置<select v-model="candidatePath" :disabled="busy"><option v-for="field in candidateFields" :key="field.path" :value="field.path">{{ field.label }}</option></select></label><p class="anchor-quote">原文：{{ candidateOriginal?.text }}</p><label>替换后的完整文本<textarea v-model="candidateValue" :disabled="busy" rows="3" /></label><button :disabled="busy || activeRun || !candidateOriginal || candidateValue === candidateOriginal.text" @click="reviewIntentCandidate">审查这份候选一次</button></div>
          </template>
          <div v-if="intentReview" class="intent-review"><h4>4 · 查看技术报告并作作者决定</h4><p class="badge">{{ creatorStatus(intentReview.report.status, "review") }}</p><ul v-if="intentReview.report.reasons.length"><li v-for="reason in intentReview.report.reasons" :key="reason">{{ reason }}</li></ul><p>修改意图：{{ creatorStatus(intentReview.report.goal_status, "requirements") }}</p><p class="muted">实际变更：{{ intentReview.report.changed_paths.map(fieldName).join('、') || '没有记录到实际变更' }}。新增技术错误{{ intentReview.report.introduced_errors.length }}项；未解决目标{{ intentReview.report.remaining_targets.length }}项。</p><p v-if="!intentReview.report.formal_results.length" class="muted">本次未添加额外条件检查。</p><details><summary>候选、字段与行为检查、软目标及结论边界</summary><pre>{{ json({ candidate: intentReview.candidate, report: intentReview.report }) }}</pre></details><div class="actions"><button class="primary" :disabled="busy || activeRun || !reviewCurrent || Boolean(intentDecision) || intentReview.report.status !== 'ready_for_author'" @click="decideIntent('accepted')">我选择这份候选</button><button :disabled="busy || activeRun || !reviewCurrent || Boolean(intentDecision)" @click="decideIntent('rejected')">我拒绝这份候选</button></div><p v-if="intentDecision" class="muted">采用情况：{{ creatorStatus(intentDecision.decision, "author") }} · 尚需单独应用。</p><button v-if="intentDecision?.decision === 'accepted'" class="primary" :disabled="busy || activeRun || !reviewCurrent || Boolean(intentReceipt)" @click="applyIntent">5 · 将已选择候选应用为新草稿</button></div>
          <div v-if="intentReceipt" class="message notice"><p>已保存草稿版本{{ intentReceipt.revision }}，未写入正典。自然语言目标：{{ label(intentReceipt.goal_status) }}。</p><details><summary>实际应用回执</summary><pre>{{ json(intentReceipt) }}</pre></details></div>
        </section>
      </main>
      <aside class="panel plan">
        <h2>本轮执行图</h2><p class="muted">显示本轮固定的步骤、指导和输入。下一轮修改另行保存。</p>
        <p v-if="graph" class="muted">{{ graph.name }} · 图版本{{ graph.version }}<span v-if="run?.skill_version_id"> · 使用流程版本</span></p>
        <p v-if="!graph" class="empty">开始运行后显示固定图快照。</p>
        <ol v-else class="nodes"><li v-for="node in graph.nodes" :key="node.id"><div><strong>{{ operation(node.operation) }}</strong><span class="badge">{{ label(nodeStatus(node.id)) }}</span></div><p v-for="parent in parents[node.id] ?? []" :key="parent" class="edge">{{ parent }}</p><details><summary>角色与方法依据</summary><p>{{ node.guidance || '本步没有额外方法指导。' }}</p><dl><dt>角色版本</dt><dd>{{ node.role_version }}</dd><dt>实际输入来源</dt><dd><pre>{{ json(node.context_bindings) }}</pre></dd></dl></details><p v-if="run?.result?.outcomes?.[node.id]?.error" class="error">{{ run.result.outcomes[node.id].error }}</p></li></ol>
      </aside>
    </div>

    <section class="panel trace">
      <div class="section-head"><h2>透明执行过程</h2><button v-if="run" :disabled="busy" @click="downloadEvidence">导出实际证据</button></div>
      <p class="muted">展示执行事实与输入来源；模型解释不能代替事实。回看事件不执行工具，也不重新发模型请求。</p>
      <div class="trace-grid">
        <ol class="timeline"><li v-for="event in visibleEvents" :key="event.seq"><span class="muted">#{{ event.seq }}</span><strong>{{ eventTitle(event) }}</strong><span v-if="event.node_id">{{ operation(event.node_id) }}</span><p v-if="eventSummary(event)">{{ eventSummary(event) }}</p><details><summary>原始事件</summary><pre>{{ json(event.payload) }}</pre></details></li></ol>
        <section class="context"><h3>每次请求实际用了什么</h3><label>模型请求<select v-model="selectedRequestId"><option value="">选择模型请求</option><option v-for="item in requests" :key="item.id" :value="item.id">{{ operation(item.node_id) }} · 第{{ item.attempt }}次派发</option></select></label><template v-if="request"><p class="muted">本地Provider请求快照；SDK未观测默认值不据此补齐。</p><details open><summary>上下文来源</summary><pre>{{ json(request.context_manifest) }}</pre></details><details><summary>已记录的实际请求</summary><pre>{{ json(request.request) }}</pre></details><details><summary>请求与上下文校验标识</summary><pre>{{ json({ request_hash: request.request_hash, context_hash: request.context_hash, scope: request.reconstruction_scope }) }}</pre></details></template><p v-else class="empty">尚无模型派发记录。</p></section>
      </div>
      <p v-if="events.length > 120" class="muted">页面显示最近120条事件；导出包含完整已保存证据。</p>
    </section>

    <section class="panel skills">
      <h2>制作流程版本</h2>
      <div class="actions"><label>流程名称（必填）<input v-model="skillName"></label><button :disabled="busy || activeRun || !run || !skillName.trim()" @click="perform(async () => { const saved = await client.saveSkill(run!.run.id, skillName.trim()); await lists(); if (saved.family) { selectedFamilyId = saved.family.id; familyChanged(); } notice = '已保存流程候选，待确认。'; })">保存本轮流程为候选</button></div>
      <div class="actions"><label>流程<select v-model="selectedFamilyId" @change="familyChanged"><option value="">按本次要求规划</option><option v-for="item in families" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label v-if="family">版本<select v-model="selectedVersionId"><option v-for="item in family.versions" :key="item.id" :value="item.id">v{{ item.version }}{{ family.default_version_id === item.id ? ' · 当前默认' : '' }} · {{ creatorStatus(item.state.author_acceptance, "author") }}</option></select></label></div>
      <div v-if="family" class="method-proposal">
        <h3>提出流程改进</h3>
        <label>流程改进要求（必填）<textarea v-model="methodText" rows="2"></textarea></label><details><summary>填写示例</summary><p class="muted">先确定选择的后果，再生成对白，减少后续逻辑改动。</p></details>
        <div class="actions"><button :disabled="busy || methodRunning || !run || !methodText.trim()" @click="proposeWorkflow()">分析当前运行并提出候选</button><button :disabled="busy" @click="perform(loadMethodEpisodes)">刷新演化记录</button><button v-if="methodJobId" :disabled="busy" @click="perform(pollMethodJob)">读取后台进度</button><span v-if="methodJobStatus" class="badge">{{ label(methodJobStatus) }}</span></div>
        <p class="muted">将根据本轮记录提出角色和交接改动，保存为待试用的候选。</p>
      </div>
      <div v-if="family && methodEpisodes.length" class="method-history">
        <h3>演化尝试与原始记录</h3>
        <article v-for="episode in methodEpisodes" :key="episode.id" class="method-episode">
          <p><strong>{{ episode.source.text }}</strong></p>
          <div class="state-badges"><span>最新结果：{{ label(episode.result?.status) }}</span><span>执行尝试：{{ episode.attempt_state.attempts.length }}</span><span>首轮留档：{{ label(episode.original_result?.status) }}</span></div>
          <p v-if="episode.result?.evidence?.error" class="error">最新问题：{{ episode.result.evidence.error }}</p>
          <p v-if="episode.original_result?.evidence?.error" class="muted">首次失败：{{ episode.original_result.evidence.error }}</p>
          <p v-if="episode.attempt_state.status === 'claimed_unknown'" class="muted">调用结果未知，须先对账；暂不能再次派发。</p>
          <template v-if="episode.attempt_state.status === 'completed_failed' || (!episode.attempt_state.attempts.length && episode.result?.status === 'failed')">
            <label>修正依据（必填）<input v-model="methodReasons[episode.id]"></label>
            <button :disabled="busy || methodRunning || !(methodReasons[episode.id] ?? '').trim()" @click="proposeWorkflow(episode)">沿原问题和预算继续尝试</button>
          </template>
          <div v-for="trial in episode.trials ?? []" :key="trial.trial_key ?? trial.candidate_version_id" class="method-trial">
            <p>候选试用：{{ label(trial.status) }}</p><p v-if="trial.error" class="error">{{ trial.error }}</p>
            <p v-if="trial.execution_observation" class="muted">{{ label(trial.execution_observation.mode) }} · 已确认provider响应{{ trial.execution_observation.confirmed_provider_responses }}次</p>
            <button v-if="trial.run_id" :disabled="busy" @click="viewMethodTrial(trial)">查看这次试用作品与实际执行</button>
            <details v-if="trial.comparison"><summary>同条件比较及结论边界</summary><pre>{{ json(trial.comparison) }}</pre></details>
          </div>
          <details><summary>查看问题身份、尝试账本与历史证据</summary><pre>{{ json(episode) }}</pre></details>
        </article>
      </div>
      <template v-if="version && family">
        <div class="state-badges"><span>运行检查：{{ creatorStatus(version.state.technical_status, "review") }}</span><span>试用：{{ creatorStatus(version.state.trial_status, "trial") }}</span><span>采用情况：{{ creatorStatus(version.state.author_acceptance, "author") }}</span><span>效果比较：{{ creatorStatus(version.state.improvement_status, "comparison") }}</span></div>
        <ul><li v-for="change in versionChanges" :key="change">{{ change }}</li></ul>
        <details><summary>查看图快照、宿主编译依据、指导与适用条件</summary><pre>{{ json({ graph: version.graph, entries: version.entries, conditions: version.conditions, compilation: version.delta?.graph_compilation }) }}</pre></details>
        <div class="actions"><button :disabled="busy || version.state.trial_status === 'allowed'" @click="perform(async () => { await client.allowTrial(version!.id, family!.family_hash, true); await lists(); notice = '已开启此版本的试用。'; })">允许这版试用</button><button :disabled="busy || version.state.trial_status !== 'allowed'" @click="perform(async () => { await client.allowTrial(version!.id, family!.family_hash, false); await lists(); notice = '已撤销试用。后续调用将重新检查许可。'; })">撤销试用许可</button><button class="primary" :disabled="busy || version.state.technical_status !== 'passed'" @click="perform(async () => { await client.decide(version!.id, family!.family_hash, 'accepted', true); await lists(); notice = '已采用，并设为后续任务的默认流程。'; })">接受方法并设为默认</button><button :disabled="busy" @click="perform(async () => { await client.decide(version!.id, family!.family_hash, 'rejected', false); await lists(); notice = '此版本未采用。'; })">不保留这版</button><button :disabled="busy || family.default_version_id === version.id || version.state.technical_status !== 'passed' || version.state.author_acceptance !== 'accepted'" @click="perform(async () => { await client.rollback(family!.id, family!.family_hash, version!.id); await lists(); notice = '后续任务将使用此版本；当前运行保留原版本。'; })">以后退回此版本</button></div>
        <p class="muted">选用或回退将在下次任务生效。设为默认将影响后续新任务。</p>
      </template>
      <p v-else class="empty">尚未选中流程。运行完成后可保存流程候选。</p>
    </section>
  </div>
</template>

<style scoped>
.evolution-studio { display: grid; gap: 1rem; max-width: 1440px; margin: 0 auto; }
.panel { border: 1px solid var(--border, #d9dedb); border-radius: 14px; padding: 1.2rem; background: var(--panel, #fff); min-width: 0; }
.panel h2 { margin: 0 0 .8rem; font-size: 1.15rem; }
.panel h3 { margin: 1rem 0 .55rem; font-size: 1rem; }
.toolbar, .actions, .section-head { display: flex; flex-wrap: wrap; gap: .7rem; align-items: center; }
.section-head { justify-content: space-between; }
label { display: grid; gap: .35rem; font-size: .9rem; }
.brief-panel { display: grid; gap: .85rem; }
input, textarea, select { background: var(--bg, #f8faf9); color: inherit; border: 1px solid var(--border, #cad3ce); border-radius: 7px; padding: .6rem; max-width: 100%; font: inherit; }
textarea { width: 100%; resize: vertical; box-sizing: border-box; line-height: 1.65; }
button { border: 1px solid var(--border, #cad3ce); border-radius: 8px; padding: .6rem .85rem; background: var(--bg, #f4f7f5); color: inherit; cursor: pointer; font: inherit; }
button.primary { background: #23664c; border-color: #23664c; color: white; }
button:disabled { opacity: .5; cursor: default; }
.work-grid { display: grid; grid-template-columns: minmax(0, 1.5fr) minmax(300px, 1fr); gap: 1rem; }
.muted, .edge { color: var(--text-muted, #64736a); font-size: .85rem; line-height: 1.6; }
.empty { padding: 1rem 0; color: var(--text-muted, #64736a); line-height: 1.8; }
.message { padding: .85rem 1rem; border-radius: 8px; margin: 0; line-height: 1.65; }
.error { color: #a43232; overflow-wrap: anywhere; }
.message.error { background: #fff0ee; border: 1px solid #edcbc7; }
.notice { background: #edf6ef; border: 1px solid #c7dfcf; }
.quest-copy p, .dialogue p, .scene { line-height: 1.85; white-space: pre-wrap; }
.dialogue article { border-bottom: 1px solid var(--border, #e1e7e3); padding: .35rem 0; }
.preview { margin: 1rem 0; padding: 1rem; border-radius: 10px; background: var(--bg, #f3f6f4); }
.choices { display: grid; gap: .5rem; margin: 1rem 0; }
.choices button { text-align: left; }
.choices small { display: block; font-size: .75rem; margin-top: .3rem; }
.feedback { border-top: 1px solid var(--border, #dae1dc); margin-top: 1.2rem; padding-top: .4rem; display: grid; gap: .75rem; }
.text-action { padding: .2rem .45rem; font-size: .78rem; background: transparent; }
.anchor-quote { border-left: 3px solid #508169; padding: .5rem .8rem; background: var(--bg, #f5f8f6); line-height: 1.7; }
.check { display: flex; align-items: center; gap: .5rem; }
.nodes { padding-left: 1.2rem; }
.nodes > li { margin-bottom: 1rem; padding: .7rem; border: 1px solid var(--border, #e0e5e2); border-radius: 9px; }
.nodes li > div { display: flex; flex-wrap: wrap; gap: .5rem; justify-content: space-between; }
.badge { display: inline-block; padding: .18rem .45rem; background: var(--bg, #f1f5f2); border-radius: 6px; font-size: .75rem; }
details { margin: .6rem 0; }
summary { cursor: pointer; font-size: .88rem; line-height: 1.7; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 28rem; overflow: auto; font-size: .76rem; line-height: 1.6; background: var(--bg, #f4f7f5); padding: .8rem; border-radius: 7px; }
dl { margin: .5rem 0; font-size: .8rem; } dt { font-weight: 600; } dd { margin-left: 0; }
.trace-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr); gap: 1.2rem; }
.timeline { max-height: 34rem; overflow: auto; padding-left: 1.3rem; }
.timeline li { padding: .5rem .4rem; border-bottom: 1px solid var(--border, #e0e5e2); }
.timeline li > span, .timeline li > strong { margin-right: .65rem; font-size: .85rem; }
.timeline p { margin: .4rem 0; font-size: .82rem; }
.context select { width: 100%; }
.state-badges { display: flex; gap: .5rem; flex-wrap: wrap; margin: 1rem 0; }
.state-badges span { background: var(--bg, #f3f6f4); padding: .5rem .7rem; border-radius: 7px; font-size: .82rem; }
.skills > .actions { margin-bottom: .9rem; }
.method-proposal, .method-episode { margin: 1rem 0; padding: 1rem; border: 1px solid var(--border, #d9dedb); border-radius: 9px; }
.method-proposal label, .method-episode label { margin-bottom: .7rem; }
.method-trial { border-left: 3px solid #508169; margin-top: .8rem; padding: .1rem .8rem; }
.shared-plan { display: grid; gap: .7rem; }
.shared-plan > p { margin: 0; }
.plan-editors { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: .3rem 1rem; }
.plan-editors details { padding: .5rem .7rem; border: 1px solid var(--border, #d9dedb); border-radius: 8px; align-self: start; }
.intent-panel { margin-top: 1.2rem; border-top: 1px solid var(--border, #dae1dc); }
.intent-panel h4 { font-size: .9rem; margin: .8rem 0; }
.intent-panel > details, .intent-contract, .candidate-edit, .intent-review { padding: .8rem; border: 1px solid var(--border, #dce3de); border-radius: 8px; margin: .8rem 0; }
.intent-panel label { margin-bottom: .7rem; }
.intent-panel select { min-width: 0; width: 100%; }
.lock-fields { max-height: 15rem; overflow: auto; }
.lock-fields label { line-height: 1.65; }
@media (max-width: 900px) { .work-grid, .trace-grid { grid-template-columns: minmax(0, 1fr); } }
@media (max-width: 650px) { .plan-editors { grid-template-columns: minmax(0, 1fr); } }
</style>
