<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { currentProject, humanizeError, llmConfig } from "../api";
import PageHead from "../components/PageHead.vue";
import StoryBranchMap from "../components/StoryBranchMap.vue";
import StoryPlaytest from "../components/StoryPlaytest.vue";
import StoryProjectPanel from "../components/StoryProjectPanel.vue";
import GeneratedStoryToolPanel from "../components/GeneratedStoryToolPanel.vue";
import StoryPatchCard from "../components/StoryPatchCard.vue";
import StoryImpactSummary from "../components/StoryImpactSummary.vue";
import StoryStageSettings from "../components/StoryStageSettings.vue";
import StoryMethodTrial from "../components/StoryMethodTrial.vue";
import StoryMethodOrigin from "../components/StoryMethodOrigin.vue";
import StorySkillPanel from "../components/StorySkillPanel.vue";
import StoryRunSummary from "../components/StoryRunSummary.vue";
import type { SkillCardSelection } from "../skillCardsApi";
import type { EvolutionArtifact, EvolutionEvent, EvolutionFamily, HarnessRequest, IntentApplicationReceipt } from "../evolutionStudioApi";
import { taskOrigin, taskSynopsis, taskTitle, type StudioPreview, type StudioTask } from "../studioApi";
import {
  CREATOR_OPERATIONS, SYSTEM_CREATOR_CHECKS, creatorStatus, storyCreatorClient,
  type ApplicationContinuation, type ApplicationDetail,
  type CollaborationMode, type CollaborationPolicy, type CreatorContext, type CreatorJob,
  type MachineRequirements, type ProductionInput, type RequirementsReport,
  type PatchSelection,
  type StoryGate, type StoryPlan, type StoryQuest, type StoryRunState, type StoryTree,
} from "../storyCreatorApi";

const project = ref(currentProject());
let api = storyCreatorClient(project.value);
let generation = 0;
let reading = 0;
let alive = true;
let timer: ReturnType<typeof setTimeout> | undefined;
const busy = ref(false);
const error = ref("");
const notice = ref("");
const context = ref<CreatorContext>({ entities: [], sources: [], narrative_projects: [] });
const tasks = ref<StudioTask[]>([]);
const taskView = ref<"author" | "development" | "unspecified">("author");
const visibleTasks = computed(() => tasks.value.filter((item) => taskOrigin(item) === taskView.value));
const task = ref<StudioTask | null>(null);
const run = ref<StoryRunState | null>(null);
const job = ref<CreatorJob | null>(null);
const jobId = ref("");
const baselineRunId = ref("");
const expectingRun = ref(false);
const brief = ref("");
const displayTitle = ref("");
const protectedText = ref("");
const selectedSources = ref<string[]>([]);
const participants = ref<string[]>([]);
const stageCount = ref("");
const decisionCount = ref("");
const optionCount = ref("");
const distinctOutcomes = ref<"" | "yes" | "no">("");
const businessRoles = ref<{ id: string; role: string; entity_id: string }[]>([]);
const savedRequirements = ref<MachineRequirements>({});
const policy = ref<CollaborationPolicy>({ mode: "delegate", plan_review: false, key_step_review: false, allow_read_tools: true, allow_candidate_trial: false });
const selection = ref<"planned" | "matching" | "template">("planned");
const skillSelections = ref<SkillCardSelection[]>([]);
const applyProjectSkills = ref(true);
const autoSummarizeSkill = ref(true);
const memoryMode = ref<"existing" | "routed">("existing");
const variant = ref<"baseline" | "constrained">("constrained");
const sourceTitle = ref("");
const sourceText = ref("");
const sourcePurpose = ref<"reference" | "project_material">("project_material");
const sourceStatus = ref<"unconfirmed" | "author_supplied">("unconfirmed");
const events = ref<EvolutionEvent[]>([]);
const requests = ref<HarnessRequest[]>([]);
const guidance = ref<Record<string, string>>({});
const gateNotes = ref("");
const gateTarget = ref("");
const gatePolicy = ref<CollaborationPolicy>({ ...policy.value });
const applicationDetail = ref<ApplicationDetail | null>(null);
const savedPreviewBeforeEdit = ref<StudioPreview | null>(null);
const previewArtifactId = ref("");
const previewPayload = ref<{ quest?: StoryQuest; tree?: StoryTree } | null>(null);
const pendingTaskPreview = ref<{ id: string; artifactId: string; payload: { quest?: StoryQuest; tree?: StoryTree }; message: string } | null>(null);
const projectPanel = ref<InstanceType<typeof StoryProjectPanel> | null>(null);
const projectDrawerOpen = ref(false);
const continuationMode = ref<"task" | "project">("task");
const selectedTrial = ref<{ version_id: string; scope_id: string; permit_hash: string } | null>(null);
const helpOpen = ref(false);
const helpEnabled = ref(true);
const preview = ref<StudioPreview | null>(null);
const previewFromArtifact = ref(false);
const appliedArtifact = ref<EvolutionArtifact | null>(null);
const patchWorking = ref(false);
const selectedNode = ref("");
const productTab = ref<"read" | "branches">("read");
const feedback = ref("");
const feedbackKind = ref<"content" | "workflow">("content");
const feedbackTarget = ref("");
const feedbackAnchor = ref<Record<string, unknown> | null>(null);
const anchoredText = ref("");
const learnFromFeedback = ref(false);
const families = ref<EvolutionFamily[]>([]);
const familyId = ref("");
const versionId = ref("");
const chosenVersionId = ref("");
const narrativeProjectId = ref("");
const narrativeSceneId = ref("");
const narrativeReady = ref(true);
const narrativePreviewId = ref("");
const narrativeTransitionId = ref("");
const skillName = ref("");
const methodJobId = ref("");
const methodJob = ref<CreatorJob | null>(null);
type EditorDraft = { revision: number; feedback: string; feedbackKind: "content" | "workflow"; feedbackTarget: string;
  gateHash?: string; gateNotes: string; gateTarget: string; guidance: Record<string, string>; gatePolicy: CollaborationPolicy };
const priorStageDraft = ref<EditorDraft | null>(null);

const modePresets: { id: CollaborationMode; name: string; promise: string; detail: string }[] = [
  { id: "delegate", name: "自动生成", promise: "按要求连续生成草稿", detail: "生成中可暂停或停止。" },
  { id: "direction", name: "先确认大纲", promise: "确认故事大纲后继续", detail: "可直接采用大纲，也可提出修改。" },
  { id: "edit", name: "分步确认", promise: "关键步骤开始前暂停", detail: "查看已有产物，再决定继续或修改。" },
];
const roleNames: Record<string, string> = { participant: "参与者", supplier: "供应者", debtor: "债务人", creditor: "债权人", buyer: "买方", seller: "卖方", beneficiary: "受益者" };
const gate = computed<StoryGate | null>(() => {
  const pending = run.value?.collaboration?.pending_gate;
  return pending ? "gate" in pending ? pending.gate : pending : null;
});
const isRunning = computed(() => ["queued", "running"].includes(run.value?.run.status ?? ""));
const isPlanning = computed(() => expectingRun.value && ["queued", "running"].includes(job.value?.status ?? "queued"));
const inFlight = computed(() => isRunning.value || isPlanning.value || Boolean(gate.value) || patchWorking.value);
const family = computed(() => families.value.find((item) => item.id === familyId.value));
const version = computed(() => family.value?.versions.find((item) => item.id === versionId.value));
const chosenVersion = computed(() => families.value.flatMap((item) => item.versions).find((item) => item.id === chosenVersionId.value));
const trialRequirements = computed(() => { try { return machine(); } catch { return null; } });
const graph = computed(() => run.value?.graph);
const creativeNodes = computed(() => (graph.value?.nodes ?? []).filter(node => !SYSTEM_CREATOR_CHECKS.has(node.operation)));
const actorKind = computed<"author" | "development_operator">(() => task.value?.origin_kind === "development" ? "development_operator" : "author");
const policyChanged = computed(() => Boolean(run.value?.collaboration) && raw(gatePolicy.value) !== raw(run.value?.collaboration?.policy));
const taskPreviewIsHistorical = computed(() => preview.value?.version_mode === "fixed_historical");
const canFeedbackAtPreview = computed(() => !taskPreviewIsHistorical.value && previewArtifactId.value === contentSource.value?.artifact_id);
const projectEditContext = computed(() => projectPanel.value?.editContinuation() ?? {});
const currentTaskPreview = computed(() => preview.value && !taskPreviewIsHistorical.value && previewArtifactId.value === contentSource.value?.artifact_id ? preview.value : null);
const continuationContext = computed<ApplicationContinuation>(() => {
  const { project_preview_id, project_preview_hash, ...projectContext } = projectEditContext.value;
  const useProject = Boolean(project_preview_id) && (continuationMode.value === "project" || !currentTaskPreview.value);
  return {
    ...(typeof feedbackAnchor.value?.path === "string" ? { reading_path: feedbackAnchor.value.path } : selectedNode.value ? { reading_path: `/tree/nodes/${pointer(selectedNode.value)}/text` } : {}),
    ...projectContext,
    ...(useProject ? { project_preview_id, project_preview_hash } : currentTaskPreview.value ? { task_preview_id: currentTaskPreview.value.preview_id, task_preview_seq: currentTaskPreview.value.view.action_seq } : {}),
  };
});
const helpText = computed(() => gate.value?.permission_required
  ? "此步骤未在当前约定中获准。可调整制作步骤许可，或停止本轮。"
  : applicationDetail.value ? "新版已保存。可核对影响，再选择新稿试玩或沿原位置继续。"
  : !task.value ? "填写创作要求即可生成；资料与制作设置可按需补充。"
  : "在剧本或试玩中定位内容后填写修改意见，可先看建议再决定是否应用。");
const changedGuidance = computed(() => Object.fromEntries(creativeNodes.value.filter((node) =>
  (guidance.value[node.id] ?? node.guidance ?? "") !== (node.guidance ?? "")).map((node) => [node.id, guidance.value[node.id] ?? ""])));
const available = computed(() => [
  ...Object.values(run.value?.result?.outcomes ?? {}).filter((item) =>
    item.status === "succeeded" || item.status === "reused").flatMap((item) =>
    Object.values(item.outputs ?? {}).map((port) => port.value)),
  ...Object.values(gate.value?.available_outputs ?? {}),
]);
function object(value: unknown): value is Record<string, unknown> { return value != null && typeof value === "object" && !Array.isArray(value); }
function isQuest(value: unknown): value is StoryQuest { return object(value) && typeof value.id === "string" && typeof value.title === "string" && typeof value.objective === "string"; }
function isTree(value: unknown): value is StoryTree { return object(value) && typeof value.id === "string" && object(value.nodes); }
function isPlan(value: unknown): value is StoryPlan { return object(value) && typeof value.premise === "string" && Array.isArray(value.beats) && value.beats.every((item) => typeof item === "string"); }
const bound = computed(() => {
  if (appliedArtifact.value) return {
    quest: isQuest(appliedArtifact.value.payload.quest) ? appliedArtifact.value.payload.quest : undefined,
    tree: isTree(appliedArtifact.value.payload.tree) ? appliedArtifact.value.payload.tree : undefined,
  };
  const complete = run.value?.result?.outputs?.bound;
  if (complete) return complete;
  const intermediate = available.value.find((item) => object(item) && isQuest(item.quest) && isTree(item.tree));
  return object(intermediate) ? { quest: intermediate.quest as StoryQuest, tree: intermediate.tree as StoryTree } : undefined;
});
const quest = computed(() => bound.value?.quest ?? run.value?.result?.outputs?.quest ?? [...available.value].reverse().find(isQuest));
const tree = computed(() => bound.value?.tree ?? [...available.value].reverse().find(isTree));
const previewQuest = computed(() => preview.value ? previewPayload.value?.quest : quest.value);
const previewTree = computed(() => preview.value ? previewPayload.value?.tree : tree.value);
const storyPlan = computed(() => run.value?.result?.outputs?.plan ?? available.value.find(isPlan));
const gateSubject = computed(() => {
  const action = graph.value?.nodes.find((node) => node.id === gate.value?.node_id)?.operation ?? "";
  if (gate.value?.stage === "planning" || action === "draft_quest") return "故事大纲";
  if (["draft_logic", "logic_contract"].includes(action)) return "场景梗概";
  if (["draft_dialogue", "dialogue_constrained"].includes(action)) return "选择与后果";
  if (["bind_dialogue", "validate_decisions", "coverage_audit", "audit", "package"].includes(action)) return "角色对白";
  return "当前草稿";
});
const gateChoices = computed(() => quest.value?.logic?.branches ?? []);
const contentSource = computed(() => appliedArtifact.value ? { artifact_id: appliedArtifact.value.id, artifact_hash: appliedArtifact.value.content_hash, kind: appliedArtifact.value.kind } : run.value?.feedback_source ?? null);
const canAnchor = computed(() => Boolean(contentSource.value) && task.value?.revision === (appliedArtifact.value?.revision ?? run.value?.run.revision));
const canPreview = computed(() => canAnchor.value && !isRunning.value && !gate.value && Boolean(bound.value));
const currentDialogue = computed(() => tree.value?.nodes[selectedNode.value]);
const readingTarget = computed(() => {
  const path = applicationDetail.value?.receipt.continuation?.reading_path;
  if (!path) return null;
  if (path === "/quest/objective" && quest.value) return { path, text: quest.value.objective };
  const stage = path.match(/^\/quest\/stages\/(0|[1-9]\d*)\/summary$/);
  const stageValue = stage ? quest.value?.stages?.[Number(stage[1])] : undefined;
  if (stageValue) return { path, text: stageValue.summary };
  const node = path.match(/^\/tree\/nodes\/([^/]+)\/(text|choices\/(\d+)\/text)$/);
  if (!node) return null;
  const key = node[1]!.replaceAll("~1", "/").replaceAll("~0", "~");
  const value = tree.value?.nodes[key];
  const text = node[2] === "text" ? value?.text : value?.choices?.[Number(node[3])]?.text;
  return text == null ? null : { path, text };
});
const patchSelection = computed<PatchSelection | null>(() => {
  const source = contentSource.value;
  const selected = feedbackAnchor.value;
  if (!source || !selected || !canAnchor.value || feedbackKind.value !== "content" || selected.artifact_id !== source.artifact_id || selected.artifact_hash !== source.artifact_hash || typeof selected.path !== "string") return null;
  const path = selected.path;
  const questValue = quest.value; const treeValue = tree.value;
  if (questValue && path === "/quest/objective") return { ...source, bundle_path: `/quests/${pointer(questValue.id)}/objective`, before_text: questValue.objective, label: "修改范围：整段场景目标" };
  if (questValue) {
    const match = path.match(/^\/quest\/stages\/(0|[1-9]\d*)\/summary$/);
    const stage = match ? questValue.stages?.[Number(match[1])] : undefined;
    if (match && stage) return { ...source, bundle_path: `/quests/${pointer(questValue.id)}/stages/${match[1]}/summary`, before_text: stage.summary, label: "修改范围：整段场景梗概" };
  }
  if (treeValue) for (const [nodeId, node] of Object.entries(treeValue.nodes)) {
    const prefix = `/tree/nodes/${pointer(nodeId)}`;
    const bundle = `/dialogue_trees/${pointer(treeValue.id)}/nodes/${pointer(nodeId)}`;
    if (path === `${prefix}/text`) return { ...source, bundle_path: `${bundle}/text`, before_text: node.text, label: "修改范围：整条对白" };
    for (const [index, choice] of (node.choices ?? []).entries()) if (path === `${prefix}/choices/${index}/text`) return { ...source, bundle_path: `${bundle}/choices/${index}/text`, before_text: choice.text, label: "修改范围：选项文字" };
  }
  return null;
});
const visitedNodes = computed(() => (Array.isArray(preview.value?.view.history) ? preview.value.view.history : [])
  .filter((entry): entry is Record<string, unknown> => object(entry)).filter((entry) => entry.phase === "dialogue" && typeof entry.cursor === "string").map((entry) => String(entry.cursor)));
const reports = computed<RequirementsReport[]>(() => {
  const report = run.value?.requirements_report;
  if (!report) return [];
  return "checks" in report && Array.isArray(report.checks) ? [report as RequirementsReport] : Object.values(report) as RequirementsReport[];
});
const requirementChecks = computed(() => reports.value.flatMap((report) => report.checks));
const methodDiff = computed(() => {
  if (!version.value) return [];
  const before = family.value?.versions.find((item) => item.id === version.value?.parent_version_id);
  if (!before) return ["首个保存版本。"];
  const oldNodes = new Map(before.graph.nodes.map((node) => [node.id, node]));
  const nextNodes = new Map(version.value.graph.nodes.map((node) => [node.id, node]));
  const changes: string[] = [];
  for (const node of version.value.graph.nodes) {
    const old = oldNodes.get(node.id);
    if (!old) changes.push(`新增“${operation(node.operation)}”`);
    else if (old.operation !== node.operation || old.role_version !== node.role_version) changes.push(`替换“${operation(node.operation)}”的制作角色`);
    else if (old.guidance !== node.guidance) changes.push(`调整“${operation(node.operation)}”的工作指导`);
  }
  for (const node of before.graph.nodes) if (!nextNodes.has(node.id)) changes.push(`移除“${operation(node.operation)}”`);
  const edgeKey = (edge: { source_node: string; source_port: string; target_node: string; target_port: string }) => `${edge.source_node}.${edge.source_port}>${edge.target_node}.${edge.target_port}`;
  const oldEdges = new Set(before.graph.data_edges.map(edgeKey));
  const nextEdges = new Set(version.value.graph.data_edges.map(edgeKey));
  const changed = [...nextEdges].filter((key) => !oldEdges.has(key)).length + [...oldEdges].filter((key) => !nextEdges.has(key)).length;
  if (changed) changes.push(`改变${changed}处步骤间的产物交接`);
  return changes.length ? changes : ["生成步骤与交接关系未变化。"];
});

function operation(value: string): string { return CREATOR_OPERATIONS[value] ?? value; }
function entityName(id: string): string { return context.value.entities.find((item) => item.id === id)?.name ?? id; }
function pointer(value: string): string { return value.replaceAll("~", "~0").replaceAll("/", "~1"); }
function readingId(path: string): string { return `creator-read-${encodeURIComponent(path)}`; }
async function showReadingTarget(): Promise<void> {
  const target = readingTarget.value; if (!target) return;
  productTab.value = "read";
  await nextTick();
  const element = document.getElementById(readingId(target.path));
  if (element) {
    let parent = element.parentElement;
    while (parent) { if (parent instanceof HTMLDetailsElement) parent.open = true; parent = parent.parentElement; }
    element.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}
function raw(value: unknown): string { return JSON.stringify(value, null, 2) ?? "未记录"; }
function jobCacheKey(taskId: string): string { return `owc_creator_job:${project.value}:${taskId}`; }
function editorKey(taskId: string): string { return `owc_creator_edits:${project.value}:${taskId}`; }
function saveEditorDraft(): void {
  if (!task.value) return;
  const draft: EditorDraft = { revision: task.value.revision, feedback: feedback.value, feedbackKind: feedbackKind.value,
    feedbackTarget: feedbackTarget.value, gateHash: gate.value?.gate_hash, gateNotes: gateNotes.value,
    gateTarget: gateTarget.value, guidance: { ...guidance.value }, gatePolicy: { ...gatePolicy.value } };
  try { sessionStorage.setItem(editorKey(task.value.id), JSON.stringify(draft)); }
  catch { notice.value = "本页未提交的意见尚未保存，请保留页面。"; }
}
function restoreEditorDraft(): void {
  if (!task.value) return;
  let draft: EditorDraft;
  try { const value = sessionStorage.getItem(editorKey(task.value.id)); if (!value) return; draft = JSON.parse(value) as EditorDraft; }
  catch { return; }
  if (draft.revision !== task.value.revision) { priorStageDraft.value = draft; return; }
  feedback.value = typeof draft.feedback === "string" ? draft.feedback : "";
  feedbackKind.value = draft.feedbackKind === "workflow" ? "workflow" : "content";
  feedbackTarget.value = typeof draft.feedbackTarget === "string" ? draft.feedbackTarget : "";
  if (draft.gateHash && draft.gateHash === gate.value?.gate_hash) {
    gateNotes.value = draft.gateNotes; gateTarget.value = draft.gateTarget; guidance.value = draft.guidance; gatePolicy.value = draft.gatePolicy;
  } else if (draft.gateNotes || draft.gateHash) priorStageDraft.value = draft;
}
function nodeStatus(id: string): string {
  const settled = run.value?.result?.outcomes?.[id]?.status;
  if (settled) return settled;
  if (gate.value?.node_id === id) return "waiting_user";
  const states: Record<string, string> = { "graph.node.started": "running", "graph.node.completed": "succeeded", "graph.node.reused": "reused", "graph.node.blocked": "blocked_dependency", "graph.node.failed": "failed", "graph.node.cancelled": "cancelled" };
  for (let index = events.value.length - 1; index >= 0; index -= 1) if (events.value[index].node_id === id && states[events.value[index].type]) return states[events.value[index].type];
  return "pending";
}
function setMode(mode: CollaborationMode): void {
  policy.value = { ...policy.value, mode, plan_review: mode !== "delegate", key_step_review: mode === "edit" };
}
function stopPolling(): void { if (timer) clearTimeout(timer); timer = undefined; }
function clearRun(): void {
  stopPolling(); run.value = null; preview.value = null; events.value = []; requests.value = [];
  applicationDetail.value = null; savedPreviewBeforeEdit.value = null; previewArtifactId.value = ""; previewPayload.value = null; pendingTaskPreview.value = null; selectedTrial.value = null; helpOpen.value = false;
  guidance.value = {}; gateNotes.value = ""; gateTarget.value = ""; selectedNode.value = "";
  feedbackAnchor.value = null; anchoredText.value = ""; expectingRun.value = false; jobId.value = ""; job.value = null;
  appliedArtifact.value = null; previewFromArtifact.value = false; patchWorking.value = false;
  feedback.value = ""; feedbackKind.value = "content"; feedbackTarget.value = ""; priorStageDraft.value = null;
}
function newDraft(): void {
  memoryMode.value = "existing";
  saveEditorDraft();
  generation += 1; clearRun(); task.value = null; brief.value = ""; displayTitle.value = ""; protectedText.value = ""; taskView.value = "author";
  participants.value = []; selectedSources.value = []; businessRoles.value = [];
  savedRequirements.value = {}; narrativePreviewId.value = ""; narrativeTransitionId.value = "";
  stageCount.value = ""; decisionCount.value = ""; optionCount.value = ""; distinctOutcomes.value = "";
  feedback.value = ""; notice.value = ""; error.value = "";
  chosenVersionId.value = "";
  skillSelections.value = [];
}
async function perform(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  const actionProject = project.value;
  busy.value = true; error.value = ""; notice.value = "";
  try { await action(); } catch (failure) { if (alive && actionProject === project.value) error.value = humanizeError(failure); }
  finally { busy.value = false; }
}
async function refreshCatalogs(): Promise<void> {
  const stamp = generation;
  const results = await Promise.allSettled([api.context(), api.tasks(), api.families()]);
  if (!alive || stamp !== generation) return;
  if (results[0].status === "fulfilled") context.value = results[0].value;
  if (results[1].status === "fulfilled") tasks.value = results[1].value.tasks;
  if (results[2].status === "fulfilled") families.value = results[2].value.families;
  const failures = results.filter((item) => item.status === "rejected");
  if (failures.length) error.value = failures.map((item) => item.status === "rejected" ? humanizeError(item.reason) : "").join("；");
}
function restoreInputs(state?: StoryRunState): void {
  memoryMode.value = state?.memory_mode === "routed" ? "routed" : "existing";
  const requirements = state?.requirements?.machine;
  savedRequirements.value = requirements ? JSON.parse(JSON.stringify(requirements)) as MachineRequirements : {};
  stageCount.value = requirements?.stage_count == null ? "" : String(requirements.stage_count);
  decisionCount.value = requirements?.decision_count == null ? "" : String(requirements.decision_count);
  optionCount.value = requirements?.decision_option_count == null ? "" : String(requirements.decision_option_count);
  distinctOutcomes.value = requirements?.distinct_outcomes == null ? "" : requirements.distinct_outcomes ? "yes" : "no";
  participants.value = requirements?.participant_ids ?? [];
  businessRoles.value = (requirements?.business_roles ?? []).map(({ id, role, entity_id }) => ({ id, role, entity_id }));
  const original = task.value?.request as { reference_ids?: string[] } | undefined;
  selectedSources.value = original?.reference_ids ?? [];
  if (state?.collaboration?.policy) policy.value = { ...state.collaboration.policy };
  chosenVersionId.value = state?.skill_version_id ?? "";
  narrativeProjectId.value = state?.narrative_project_id ?? "";
  narrativeSceneId.value = state?.narrative_scene_id ?? "";
  narrativePreviewId.value = state?.narrative_preview_id ?? "";
  narrativeTransitionId.value = state?.narrative_transition_id ?? "";
}
async function openTask(id: string): Promise<void> {
  saveEditorDraft();
  skillSelections.value = [];
  generation += 1; clearRun(); error.value = ""; notice.value = "";
  narrativePreviewId.value = ""; narrativeTransitionId.value = "";
  const stamp = generation;
  const [detail, stored] = await Promise.all([api.detail(id), api.taskArtifacts(id)]);
  if (!alive || stamp !== generation) return;
  task.value = detail.task; taskView.value = taskOrigin(detail.task); displayTitle.value = taskTitle(detail.task); brief.value = detail.task.request.brief; protectedText.value = detail.task.request.protected.join("\n");
  run.value = detail.runs[0] ?? null; restoreInputs(run.value ?? undefined);
  appliedArtifact.value = stored.artifacts.filter((artifact) => artifact.kind === "bound_dialogue" && artifact.revision === detail.task.revision && !artifact.stale).at(-1) ?? null;
  const saved = sessionStorage.getItem(jobCacheKey(id));
  if (saved) {
    try {
      const pending: unknown = JSON.parse(saved);
      if (object(pending) && typeof pending.job_id === "string" && typeof pending.baseline_run_id === "string") {
        jobId.value = pending.job_id; baselineRunId.value = pending.baseline_run_id; expectingRun.value = true;
      }
    } catch { sessionStorage.removeItem(jobCacheKey(id)); }
  }
  if (run.value || jobId.value) await poll();
  await recoverApplication(id);
  if (alive && stamp === generation) restoreEditorDraft();
}
function chooseRun(state: StoryRunState): void {
  if (run.value?.run.id !== state.run.id) { events.value = []; requests.value = []; preview.value = null; feedbackAnchor.value = null; anchoredText.value = ""; appliedArtifact.value = null; previewFromArtifact.value = false; }
  run.value = state;
}
async function poll(): Promise<void> {
  const currentTask = task.value;
  if (!currentTask) return;
  const stamp = generation; const order = ++reading; const client = api;
  const [detail, work, learning] = await Promise.all([
    client.detail(currentTask.id), jobId.value ? client.job(jobId.value) : Promise.resolve(null),
    methodJobId.value ? client.job(methodJobId.value) : Promise.resolve(null),
  ]);
  if (!alive || stamp !== generation || order !== reading) return;
  task.value = detail.task; job.value = work; methodJob.value = learning;
  if (expectingRun.value) {
    const next = detail.runs.find((item) => item.run.id !== baselineRunId.value);
    if (next) { chooseRun(next); expectingRun.value = false; }
  } else if (run.value) {
    const state = detail.runs.find((item) => item.run.id === run.value?.run.id);
    if (state) chooseRun(state);
  } else if (detail.runs[0]) chooseRun(detail.runs[0]);
  const runId = run.value?.run.id;
  if (runId) {
    let cursor = events.value.at(-1)?.seq ?? 0;
    for (let page = 0; page < 4; page += 1) {
      const batch = (await client.events(runId, cursor)).events;
      if (!alive || stamp !== generation || order !== reading || run.value?.run.id !== runId) return;
      if (!batch.length) break;
      const seen = new Set(events.value.map((event) => event.seq));
      events.value = [...events.value, ...batch.filter((event) => !seen.has(event.seq))].sort((left, right) => left.seq - right.seq);
      cursor = batch.at(-1)?.seq ?? cursor;
      if (batch.length < 200) break;
    }
  }
  if (work?.status === "failed") { error.value = work.error || "后台制作失败，没有自动重试。"; expectingRun.value = false; }
  else if (work && expectingRun.value && !["queued", "running"].includes(work.status)) {
    expectingRun.value = false;
    error.value = typeof work.result?.error === "string" ? work.result.error : "后台作业已经结束，但还没有读到新的可执行运行。已有作品保留，请读取任务确认实际结果。";
  }
  if (work && !["queued", "running"].includes(work.status)) sessionStorage.removeItem(jobCacheKey(currentTask.id));
  if (learning && !["queued", "running"].includes(learning.status)) {
    if (learning.status === "failed") error.value = learning.error || "方法提议失败，已有作品仍保留。";
    methodJobId.value = ""; await refreshCatalogs();
    if (!alive || stamp !== generation) return;
  }
  stopPolling();
  if (isRunning.value || isPlanning.value || Boolean(methodJobId.value)) timer = setTimeout(() => { void poll().catch((failure) => {
    if (alive && stamp === generation) { stopPolling(); error.value = `自动读取已停止：${humanizeError(failure)}。可手动读取当前任务；不会重发模型请求。`; }
  }); }, 2500);
}
function numberInput(value: string, name: string, min: number, max: number): number | undefined {
  if (value.trim() === "") return undefined;
  const number = Number(value);
  if (!Number.isInteger(number) || number < min || number > max) throw new Error(`${name}须为${min}至${max}的整数；也可留空。`);
  return number;
}
function machine(): MachineRequirements {
  const stages = numberInput(stageCount.value, "本次阶段数", 1, 64);
  const decisions = numberInput(decisionCount.value, "本次决策数", 0, 10);
  const options = numberInput(optionCount.value, "每个决策的选项数", 2, 6);
  if (decisions === 0 && (options != null || distinctOutcomes.value === "yes")) throw new Error("决策数为零时，不能同时要求每次选项数或不同选择后果。");
  if (businessRoles.value.some((item) => !item.entity_id)) throw new Error("请为每项人物关系选择真实项目人物，或删除未填项。");
  return {
    ...(savedRequirements.value.schema_version ? { schema_version: savedRequirements.value.schema_version } : {}),
    ...(savedRequirements.value.game_state_variables ? { game_state_variables: savedRequirements.value.game_state_variables } : {}),
    ...(savedRequirements.value.business_relations ? { business_relations: savedRequirements.value.business_relations } : {}),
    ...(stages == null ? {} : { stage_count: stages }), ...(decisions == null ? {} : { decision_count: decisions }),
    ...(options == null ? {} : { decision_option_count: options }),
    ...(distinctOutcomes.value ? { distinct_outcomes: distinctOutcomes.value === "yes" } : {}), participant_ids: [...participants.value],
    business_roles: businessRoles.value.map((item) => {
      const original = savedRequirements.value.business_roles?.find((old) => old.id === item.id && old.role === item.role && old.entity_id === item.entity_id);
      return { ...item, source_refs: original ? [...original.source_refs] : ["author_structured"] };
    }),
  };
}
async function startProduction(): Promise<void> {
  const current = task.value;
  if (!current) return;
  if (narrativeProjectId.value && !narrativeReady.value) throw new Error("作品结构或场景引用尚未保存，请先在章节与场景区保存，再开始制作。");
  const input: ProductionInput = {
    selection: selection.value, variant: variant.value, machine_requirements: machine(), collaboration_policy: { ...policy.value },
    apply_project_skills: applyProjectSkills.value,
    summarize_skill: autoSummarizeSkill.value,
    memory_mode: memoryMode.value,
    skill_card_selections: skillSelections.value.map(item => ({ ...item })),
    ...(chosenVersion.value ? { skill_version_id: chosenVersion.value.id } : {}),
    ...(selectedTrial.value && selectedTrial.value.version_id === chosenVersion.value?.id ? { trial_scope_id: selectedTrial.value.scope_id, expected_trial_scope_hash: selectedTrial.value.permit_hash } : {}), ...(run.value ? { prior_run_id: run.value.run.id } : {}),
    ...(narrativeProjectId.value ? { narrative_project_id: narrativeProjectId.value, ...(narrativeSceneId.value ? { narrative_scene_id: narrativeSceneId.value } : {}) } : {}),
    ...(narrativePreviewId.value ? { narrative_preview_id: narrativePreviewId.value, ...(narrativeTransitionId.value ? { narrative_transition_id: narrativeTransitionId.value } : {}) } : {}),
  };
  const stamp = generation;
  const previous = run.value?.run.id ?? "";
  const response = await api.produce(current.id, input);
  if (!alive || stamp !== generation) return;
  skillSelections.value = [];
  baselineRunId.value = previous; expectingRun.value = true; jobId.value = response.job_id;
  sessionStorage.setItem(jobCacheKey(current.id), JSON.stringify({ job_id: response.job_id, baseline_run_id: previous }));
  job.value = { job_id: response.job_id, status: "queued", kind: "creative_production", events: [] };
  if (response.run_id) { chooseRun({ run: { id: response.run_id, revision: current.revision, status: "queued" } }); expectingRun.value = false; }
  notice.value = "已开始生成。离开页面后可从故事列表继续查看。";
  await poll();
}
async function createAndProduce(): Promise<void> {
  await perform(async () => {
    if (!brief.value.trim()) throw new Error("先写下一个灵感、片段或制作目的。");
    machine(); // Validate only the explicit form before creating a persistent task.
    if (narrativeProjectId.value && !narrativeReady.value) throw new Error("作品结构或场景引用尚未保存，请先在章节与场景区保存。");
    const stamp = generation;
    const model = llmConfig().model;
    const created = await api.createTask({ brief: brief.value.trim(), ...(displayTitle.value.trim() ? { display_title: displayTitle.value.trim() } : {}), origin_kind: "author", protected: protectedText.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean),
      participation: policy.value.mode, use_references: selectedSources.value.length > 0, reference_ids: [...selectedSources.value], ...(model ? { model } : {}) });
    if (!alive || stamp !== generation) return;
    clearRun(); task.value = created.task;
    await refreshCatalogs(); await startProduction();
  });
}
async function addSource(): Promise<void> {
  await perform(async () => {
    if (!sourceTitle.value.trim() || !sourceText.value.trim()) throw new Error("请给资料一个名称并粘贴实际内容。");
    const stamp = generation;
    const response = await api.addSource({ title: sourceTitle.value.trim(), text: sourceText.value, allowed_uses: ["drafting"], purpose: sourcePurpose.value, canon_status: sourceStatus.value });
    if (!alive || stamp !== generation) return;
    await refreshCatalogs(); selectedSources.value = [...new Set([...selectedSources.value, response.source.id])];
    sourceTitle.value = ""; sourceText.value = "";
    notice.value = "资料已添加，并选入新故事。";
  });
}
async function pause(): Promise<void> {
  await perform(async () => {
    const current = run.value;
    if (!current?.collaboration) return;
    const stamp = generation;
    await api.pause(current.run.id, current.collaboration.policy_hash);
    if (!alive || stamp !== generation) return;
    await poll();
  notice.value = "已请求暂停，将在当前步骤结束后停下。";
  });
}
async function resume(action: "continue" | "revise" | "cancel"): Promise<void> {
  await perform(async () => {
    const current = run.value; const checkpoint = gate.value;
    if (!current || !checkpoint) return;
    const changes = { ...changedGuidance.value };
    if (gateNotes.value.trim()) {
      const target = gateTarget.value || checkpoint.node_id;
      changes[target] = `${guidance.value[target] ?? graph.value?.nodes.find((node) => node.id === target)?.guidance ?? ""}\n本次调整意见：${gateNotes.value.trim()}`.trim();
    }
    if (action === "revise" && !Object.keys(changes).length && !policyChanged.value) throw new Error("请先改一条计划指导，或说明这一步希望如何调整。");
    if (action === "continue" && (Object.keys(changes).length || policyChanged.value)) throw new Error("当前有尚未提交的调整。请使用“按我的调整重新制作”，或先撤回编辑。");
    const stamp = generation;
    const response = await api.resume(current.run.id, { expected_gate_hash: checkpoint.gate_hash, action, actor_kind: actorKind.value, ...(action === "revise" ? { guidance: changes, ...(policyChanged.value ? { collaboration_policy: gatePolicy.value, expected_policy_hash: current.collaboration!.policy_hash } : {}) } : {}) });
    if (!alive || stamp !== generation) return;
    if (response.run_id !== current.run.id) chooseRun({ run: { id: response.run_id, revision: current.run.revision, status: "queued" } });
    if (response.job_id) jobId.value = response.job_id;
    gateNotes.value = ""; notice.value = action === "cancel" ? "已停止本轮，已生成内容保留。" : action === "revise" ? "已保存修改，开始生成新版本。" : "已确认，继续生成。";
    await poll();
  });
}
async function startPreview(): Promise<void> {
  await perform(async () => {
    if (!run.value || !canPreview.value) return;
    const stamp = generation;
    const artifactMode = Boolean(appliedArtifact.value);
    const result = appliedArtifact.value && task.value ? await api.previewArtifact(task.value.id, appliedArtifact.value.id) : await api.preview(run.value.run.id);
    if (alive && stamp === generation) { preview.value = result; previewFromArtifact.value = artifactMode; previewArtifactId.value = contentSource.value?.artifact_id ?? ""; previewPayload.value = { quest: quest.value, tree: tree.value }; }
  });
}
async function step(choiceId?: string): Promise<void> {
  await perform(async () => {
    if (!preview.value) return;
    const stamp = generation;
    const advance = taskPreviewIsHistorical.value ? api.stepHistoricalTask : previewFromArtifact.value ? api.stepArtifact : api.step;
    const result = await advance(preview.value.preview_id, { action_id: crypto.randomUUID(), expected_seq: preview.value.view.action_seq, ...(choiceId ? { choice_id: choiceId } : {}) });
    if (alive && stamp === generation) preview.value = normalizeTaskPreview(result);
  });
}
function anchor(path: string, text: string, fromPreview = false): void {
  const source = contentSource.value;
  if (!source || !canAnchor.value) return;
  feedbackKind.value = "content"; feedbackTarget.value = ""; anchoredText.value = text;
  feedbackAnchor.value = { artifact_id: source.artifact_id, artifact_hash: source.artifact_hash, path,
    ...(fromPreview && preview.value ? { preview_id: preview.value.preview_id, action_seq: preview.value.view.action_seq } : {}) };
}
function patchApplied(value: { artifact: EvolutionArtifact; task: StudioTask; receipt: IntentApplicationReceipt }): void {
  const continued = value.receipt.continuation;
  savedPreviewBeforeEdit.value = preview.value && !taskPreviewIsHistorical.value
    && previewArtifactId.value === value.receipt.source_artifact_id
    && continued?.task_preview_id === preview.value.preview_id
    && continued.task_preview_seq === preview.value.view.action_seq ? preview.value : null;
  appliedArtifact.value = value.artifact; task.value = value.task;
  tasks.value = tasks.value.map((item) => item.id === value.task.id ? value.task : item);
  applicationDetail.value = { ...value, impact: value.receipt.impact ?? {}, target_current: true };
  feedbackAnchor.value = null; anchoredText.value = "";
  feedback.value = "";
  const reading = value.receipt.continuation?.reading_path;
  if (reading) restoreReading(reading);
  notice.value = `已载入草稿 v${value.receipt.revision}。原试玩仍使用旧稿。`;
}
function normalizeTaskPreview(value: unknown): StudioPreview {
  if (!object(value) || typeof value.preview_id !== "string") throw new Error("试玩响应缺少会话身份。");
  const view = object(value.view) ? value.view : value;
  if (!Number.isInteger(view.action_seq) || !Array.isArray(view.choices)) throw new Error("试玩位置响应不完整，请重新读取会话。");
  return { preview_id: value.preview_id, view: view as StudioPreview["view"],
    ...(value.version_mode === "fixed_historical" ? { version_mode: "fixed_historical" as const } : {}) };
}
function restoreReading(path: string): void {
  const match = path.match(/^\/tree\/nodes\/([^/]+)\/(?:text|choices\/\d+\/text)$/);
  if (!match) return;
  const key = match[1]!.replaceAll("~1", "/").replaceAll("~0", "~");
  if (tree.value?.nodes[key]) { selectedNode.value = key; productTab.value = "read"; }
}
async function recoverApplication(taskId: string): Promise<void> {
  const stamp = generation;
  try {
    const records = (await api.applications(taskId)).applications;
    if (!alive || stamp !== generation || task.value?.id !== taskId) return;
    const latest = records.filter((item) => item.revision === task.value?.revision).at(-1);
    if (!latest) return;
    const result = await api.application(latest.id);
    if (!alive || stamp !== generation || task.value?.id !== taskId || !result.target_current) return;
    applicationDetail.value = result; appliedArtifact.value = result.artifact;
    if (result.receipt.continuation?.reading_path) restoreReading(result.receipt.continuation.reading_path);
    const originalId = result.receipt.continuation?.task_preview_id;
    if (originalId && !preview.value) {
      const original = normalizeTaskPreview(await api.readTaskPreview(originalId));
      const artifactId = result.receipt.source_artifact_id;
      const source = artifactId ? (await api.artifact(artifactId)).artifact : null;
      if (!alive || stamp !== generation || task.value?.id !== taskId) return;
      savedPreviewBeforeEdit.value = original; preview.value = original; previewArtifactId.value = artifactId ?? "";
      previewPayload.value = source ? { quest: isQuest(source.payload.quest) ? source.payload.quest : undefined, tree: isTree(source.payload.tree) ? source.payload.tree : undefined } : null;
    }
  } catch (failure) { if (alive && stamp === generation) error.value = `修改记录读取未完成：${humanizeError(failure)}`; }
}
async function continueEdited(): Promise<void> {
  await perform(async () => {
    const detail = applicationDetail.value;
    const source = savedPreviewBeforeEdit.value;
    if (!detail || !source || pendingTaskPreview.value) return;
    const stamp = generation;
    const fork = await api.continueEditedPreview(detail.receipt.id, source.preview_id, source.view.action_seq);
    if (!alive || stamp !== generation) return;
    pendingTaskPreview.value = { id: normalizeTaskPreview(fork).preview_id, artifactId: detail.artifact.id,
      payload: { quest: quest.value, tree: tree.value }, message: "已在新稿继续原位置，原会话保留。" };
    await readPendingTaskPreview();
  });
}
async function continueOriginalTask(): Promise<void> {
  await perform(async () => {
    const source = savedPreviewBeforeEdit.value ?? preview.value;
    if (!source || pendingTaskPreview.value) return;
    const stamp = generation;
    const sourceId = savedPreviewBeforeEdit.value?.preview_id === source.preview_id
      ? applicationDetail.value?.receipt.source_artifact_id ?? previewArtifactId.value : previewArtifactId.value;
    const sourceArtifact = sourceId ? (await api.artifact(sourceId)).artifact : null;
    const fork = normalizeTaskPreview(await api.continueOriginalTask(source.preview_id, source.view.action_seq));
    if (!alive || stamp !== generation) return;
    pendingTaskPreview.value = { id: fork.preview_id, artifactId: sourceId,
      payload: sourceArtifact ? { quest: isQuest(sourceArtifact.payload.quest) ? sourceArtifact.payload.quest : undefined, tree: isTree(sourceArtifact.payload.tree) ? sourceArtifact.payload.tree : undefined } : {}, message: "当前试玩固定使用旧稿，不改变新版内容。" };
    await readPendingTaskPreview();
  });
}
async function readPendingTaskPreview(): Promise<void> {
  const pending = pendingTaskPreview.value; const stamp = generation;
  if (!pending) return;
  const latest = normalizeTaskPreview(await api.readTaskPreview(pending.id));
  if (!alive || stamp !== generation || pendingTaskPreview.value?.id !== pending.id) return;
  preview.value = latest; previewArtifactId.value = pending.artifactId; previewPayload.value = pending.payload;
  previewFromArtifact.value = !taskPreviewIsHistorical.value; pendingTaskPreview.value = null; notice.value = pending.message;
}
async function openApplicationProject(): Promise<void> {
  await perform(async () => {
    const context = applicationDetail.value?.receipt.continuation;
    if (!context?.narrative_project_id) return;
    projectDrawerOpen.value = true;
    await projectPanel.value?.openEditContext(context);
    notice.value = "已打开原场景。绑定新版后可检查原路线是否能够继续。";
  });
}
function useTrial(value: { version_id: string; scope_id: string; permit_hash: string } | null): void {
  const previous = selectedTrial.value;
  selectedTrial.value = value;
  if (value) chosenVersionId.value = value.version_id;
  else if (previous && chosenVersionId.value === previous.version_id) chosenVersionId.value = "";
}
function feedbackAtPreview(): void {
  if (!canFeedbackAtPreview.value) return;
  const view = preview.value?.view;
  if (view?.phase === "dialogue" && typeof view.cursor === "string") anchor(`/tree/nodes/${pointer(view.cursor)}/text`, view.text ?? "", true);
  else if (view?.phase === "quest") {
    const index = quest.value?.stages?.findIndex((stage) => stage.id === view.cursor) ?? -1;
    if (index >= 0) anchor(`/quest/stages/${index}/summary`, view.text ?? "", true);
  }
}
function legacyOperation(value: string): string { return ({ logic_contract: "draft_logic", validate_decisions: "draft_logic", dialogue_constrained: "draft_dialogue", coverage_audit: "audit" } as Record<string, string>)[value] ?? value; }
async function reviseFromFeedback(): Promise<void> {
  await perform(async () => {
    const current = task.value; const sourceRun = run.value?.run.id; const selectedFamily = family.value;
    if (!current || !feedback.value.trim()) return;
    const text = feedback.value.trim(); const stamp = generation;
    const response = await api.feedback(current.id, { expected_revision: current.revision, text, scope: feedbackKind.value,
      ...(feedbackAnchor.value && feedbackKind.value === "content" ? { anchor: feedbackAnchor.value } : feedbackTarget.value ? { target_operation: legacyOperation(feedbackTarget.value) } : {}) });
    if (!alive || stamp !== generation) return;
    task.value = response.task; preview.value = null; feedbackAnchor.value = null; anchoredText.value = ""; feedback.value = "";
    notice.value = "意见已保存为新任务版本，正在开始实际修订。";
    if (learnFromFeedback.value && selectedFamily && sourceRun) {
      try { const proposed = await api.evolve(selectedFamily.id, sourceRun, text, undefined, actorKind.value === "development_operator" ? "development_input" : "author_feedback"); methodJobId.value = proposed.job_id ?? ""; }
      catch (failure) { error.value = `作品反馈已保存；方法候选未提交：${humanizeError(failure)}`; }
    }
    await startProduction();
  });
}
async function saveMethod(): Promise<void> {
  await perform(async () => {
    if (!run.value || !skillName.value.trim()) return;
    const stamp = generation;
    const saved = await api.saveSkill(run.value.run.id, skillName.value.trim());
    if (!alive || stamp !== generation) return;
    await refreshCatalogs();
    if (!alive || stamp !== generation) return;
    if (saved.family) { familyId.value = saved.family.id; versionId.value = saved.version?.id ?? saved.family.default_version_id ?? family.value?.versions.at(-1)?.id ?? ""; }
    notice.value = "已保存流程候选，待确认。";
  });
}
async function methodAction(action: "accept" | "reject" | "rollback"): Promise<void> {
  await perform(async () => {
    const current = family.value; const candidate = version.value;
    if (!current || !candidate) return;
    const stamp = generation;
    if (action === "rollback") await api.rollback(current.id, current.family_hash, candidate.id);
    else await api.decide(candidate.id, current.family_hash, action === "accept" ? "accepted" : "rejected", action === "accept");
    if (!alive || stamp !== generation) return;
    if (action === "reject" && chosenVersionId.value === candidate.id) { chosenVersionId.value = ""; selectedTrial.value = null; }
    await refreshCatalogs(); notice.value = action === "rollback" ? "后续任务将使用选定版本。" : action === "accept" ? "已采用，并设为后续任务的默认流程。" : "此版本未采用。";
  });
}
async function loadDebug(event: Event): Promise<void> {
  if (!(event.target instanceof HTMLDetailsElement) || !event.target.open || !run.value) return;
  const stamp = generation; const runId = run.value.run.id;
  try { const result = await api.requests(runId); if (alive && stamp === generation && run.value?.run.id === runId) requests.value = result.requests; }
  catch (failure) { if (alive && stamp === generation) error.value = humanizeError(failure); }
}
async function downloadEvidence(): Promise<void> {
  await perform(async () => {
    if (!run.value) return;
    const runId = run.value.run.id; const stamp = generation; const evidence = await api.evidence(runId);
    if (!alive || stamp !== generation) return;
    const url = URL.createObjectURL(new Blob([raw(evidence)], { type: "application/json" }));
    const link = document.createElement("a"); link.href = url; link.download = `story-production-${runId}.json`; link.click(); URL.revokeObjectURL(url);
  });
}
function changeFamily(): void { versionId.value = family.value?.default_version_id ?? family.value?.versions.at(-1)?.id ?? ""; }
function chooseNarrativeProject(projectId: string, sceneId: string, ready: boolean, path?: { preview_id: string; transition_id?: string }): void {
  narrativeProjectId.value = projectId; narrativeSceneId.value = sceneId; narrativeReady.value = ready;
  narrativePreviewId.value = path?.preview_id ?? ""; narrativeTransitionId.value = path?.transition_id ?? "";
}
function addBusinessRole(): void { businessRoles.value.push({ id: `role_${crypto.randomUUID().replaceAll("-", "")}`, role: "participant", entity_id: "" }); }
function onStorage(event: StorageEvent): void {
  if (event.key !== "owcopilot_project") return;
  newDraft(); project.value = currentProject(); api = storyCreatorClient(project.value);
  tasks.value = []; families.value = []; context.value = { entities: [], sources: [], narrative_projects: [] }; familyId.value = ""; versionId.value = "";
  methodJobId.value = ""; methodJob.value = null; void refreshCatalogs();
  narrativeProjectId.value = ""; narrativeSceneId.value = "";
}
watch(() => run.value?.graph_hash, () => { guidance.value = Object.fromEntries((graph.value?.nodes ?? []).map((node) => [node.id, node.guidance ?? ""])); });
watch(() => gate.value?.gate_hash, () => { gateNotes.value = ""; gateTarget.value = creativeNodes.value.find(node => node.id === gate.value?.node_id)?.id ?? creativeNodes.value[0]?.id ?? ""; gatePolicy.value = { ...(run.value?.collaboration?.policy ?? policy.value) }; });
watch(() => `${tree.value?.id ?? ""}:${Object.keys(tree.value?.nodes ?? {}).join("|")}`, () => {
  if (!selectedNode.value || !tree.value?.nodes[selectedNode.value]) selectedNode.value = tree.value?.root_node ?? Object.keys(tree.value?.nodes ?? {})[0] ?? "";
});
onMounted(() => { window.addEventListener("storage", onStorage); window.addEventListener("beforeunload", saveEditorDraft); void refreshCatalogs(); });
onUnmounted(() => { saveEditorDraft(); alive = false; generation += 1; stopPolling(); window.removeEventListener("storage", onStorage); window.removeEventListener("beforeunload", saveEditorDraft); });
</script>

<template>
  <div class="creator">
    <PageHead overline="STORY WORKSPACE" title="剧情策划室" purpose="编写、试玩和修改剧情。"><template #aside><button v-if="helpEnabled" @click="helpOpen = !helpOpen">帮助</button><button v-else @click="helpEnabled = true; helpOpen = true">开启帮助</button><router-link class="quiet-link" to="/evolution">打开流程调试台 ↗</router-link></template></PageHead>
    <aside v-if="helpEnabled && helpOpen" class="message notice"><p>{{ helpText }}</p><div class="compact-actions"><button @click="helpOpen = false">稍后再看</button><button @click="helpOpen = false; helpEnabled = false">关闭帮助</button></div></aside>
    <p v-if="error" class="message error" role="alert">{{ error }}</p>
    <p v-if="notice" class="message notice" role="status">{{ notice }}</p>
    <div class="workspace">
      <aside class="project-rail">
        <div class="rail-title"><span>故事</span><button :disabled="busy || inFlight" @click="newDraft">＋</button></div>
        <button class="new-story" :class="{ selected: !task }" :disabled="busy || inFlight" @click="newDraft">新建故事</button>
        <label class="task-view">显示<select v-model="taskView"><option value="author">我的故事</option><option value="unspecified">未标记来源</option><option value="development">开发记录</option></select></label><div class="task-list"><button v-for="item in visibleTasks" :key="item.id" :class="{ selected: item.id === task?.id }" :disabled="busy" @click="perform(() => openTask(item.id))"><span>{{ taskTitle(item) }}</span><small>草稿版本 {{ item.revision }}</small></button></div>
        <button class="rail-refresh" :disabled="busy" @click="perform(refreshCatalogs)">刷新任务与资料</button>
        <p v-if="!visibleTasks.length" class="hint">此分类暂无记录。</p><p class="rail-note">工作区：{{ project }}</p>
      </aside>
      <main class="creator-main">
        <section class="card creative-brief">
          <header class="section-title"><div><span class="eyebrow">01 / CREATIVE BRIEF</span><h2>{{ task ? taskTitle(task) : '新建故事' }}</h2></div><span v-if="task" class="subtle-badge">草稿 v{{ task.revision }}</span></header>
          <template v-if="!task"><label>故事名称（选填）<input v-model="displayTitle" maxlength="200"></label><label class="large-label" for="story-idea">创作要求（必填）</label><textarea id="story-idea" v-model="brief" class="idea-input" rows="4" maxlength="16000" /></template>
          <template v-else><p v-if="taskSynopsis(task)" class="brief-snapshot">{{ taskSynopsis(task) }}</p><details><summary>原始创作要求</summary><p class="brief-snapshot">{{ task.request.brief }}</p></details></template>
          <details class="setup-details" :open="!task"><summary>资料与生成设置</summary>
            <div class="setup-grid"><section><h3>参考资料</h3><div class="source-list"><label v-for="source in context.sources" :key="source.id" class="check source-item"><input v-model="selectedSources" type="checkbox" :value="source.id" :disabled="Boolean(task) || source.quarantine_status === 'quarantined' || !source.allowed_uses.includes('drafting')"><span>{{ source.title }}<small>{{ source.metadata?.purpose === 'project_material' ? '项目资料' : '参考来源' }} · {{ source.char_count ?? '未知' }}字<span v-if="source.quarantine_status === 'quarantined'"> · 当前不可用</span></small></span></label><p v-if="!context.sources.length" class="hint">暂无资料。可添加角色档案、前文或参考片段。</p></div>
              <details v-if="!task" class="add-source"><summary>＋ 添加文本资料</summary><label>资料名称（必填）<input v-model="sourceTitle" maxlength="200"></label><label>用途<select v-model="sourcePurpose"><option value="project_material">项目制作资料</option><option value="reference">灵感参考</option></select></label><label>材料来源<select v-model="sourceStatus" aria-describedby="creator-source-adoption"><option value="unconfirmed">尚未确认</option><option value="author_supplied">由我提供的创作材料</option></select></label><label>资料正文（必填）<textarea v-model="sourceText" rows="5" /></label><button :disabled="busy || !sourceTitle.trim() || !sourceText.trim()" @click="addSource">添加并选用</button><p id="creator-source-adoption" class="hint">添加资料不会自动采用为设定。</p></details>
              <label>必须保留的内容（选填，每行一项）<textarea v-model="protectedText" :disabled="Boolean(task)" rows="3" /></label>
            </section><section><h3>草稿要求（选填）</h3><p id="creator-count-format" class="hint">留空不指定；阶段1–64，选择0–10，选项2–6。</p><div class="count-fields"><label>阶段数<input aria-describedby="creator-count-format" v-model="stageCount" type="number" min="1" max="64"></label><label>选择次数<input aria-describedby="creator-count-format" v-model="decisionCount" type="number" min="0" max="10"></label><label>每次选项数<input aria-describedby="creator-count-format" v-model="optionCount" type="number" min="2" max="6"></label></div><label>选项后果<select v-model="distinctOutcomes"><option value="">尚未指定</option><option value="yes">必须有不同后果</option><option value="no">不作此项要求</option></select></label>
              <details v-if="savedRequirements.game_state_variables?.length || savedRequirements.business_relations?.length"><summary>已保存的状态与关系要求</summary><p class="hint">本次制作继续保留这些要求。</p><pre>{{ raw({ game_state_variables: savedRequirements.game_state_variables, business_relations: savedRequirements.business_relations }) }}</pre></details><details><summary>人物与关系</summary><div class="entity-list"><label v-for="entity in context.entities" :key="entity.id" class="check"><input v-model="participants" type="checkbox" :value="entity.id">{{ entity.name }} <small>{{ entity.kind }}</small></label><p v-if="!context.entities.length" class="hint">暂无项目人物。可在创作要求中描述新人物。</p></div><div v-for="(binding, index) in businessRoles" :key="binding.id" class="role-row"><label>人物<select v-model="binding.entity_id"><option value="">选择项目人物</option><option v-for="entity in context.entities" :key="entity.id" :value="entity.id">{{ entity.name }}</option></select></label><label>关系<select v-model="binding.role"><option v-for="(name, key) in roleNames" :key="key" :value="key">{{ name }}</option></select></label><button aria-label="删除人物关系" @click="businessRoles.splice(index, 1)">×</button></div><button :disabled="!context.entities.length" @click="addBusinessRole">＋ 添加人物关系</button></details>
            </section></div>
            <section class="collaboration-setup"><h3>{{ run ? '下次生成的确认方式' : '确认方式' }}</h3><div class="mode-cards"><button v-for="mode in modePresets" :key="mode.id" :class="{ selected: policy.mode === mode.id }" @click="setMode(mode.id)"><span class="radio-mark" aria-hidden="true">{{ policy.mode === mode.id ? '●' : '○' }}</span><strong>{{ mode.name }}</strong><span>{{ mode.promise }}</span><small>{{ mode.detail }}</small></button></div><details><summary>更多协作设置</summary><div class="policy-toggles"><label class="check"><input v-model="policy.plan_review" type="checkbox">生成场景前确认大纲</label><label class="check"><input v-model="policy.key_step_review" type="checkbox">关键步骤开始前确认</label><label class="check"><input v-model="policy.allow_read_tools" type="checkbox">允许工具读取所选项目资料</label><label class="check"><input v-model="policy.allow_candidate_trial" type="checkbox" aria-describedby="creator-trial-scope">允许试用新的制作流程</label></div><p id="creator-trial-scope" class="hint">此许可用于本次运行，不改变后续默认流程。</p><StoryStageSettings v-model="policy" :disabled="busy" /></details></section>
            <details><summary>高级流程设置</summary><label>步骤安排<select v-model="selection"><option value="planned">按本次任务安排步骤</option><option value="matching">匹配已采用的工作图</option><option value="template">选择基础流程</option></select></label><label v-if="selection === 'template'">基础模板<select v-model="variant"><option value="constrained">先设计状态后果，再写对白</option><option value="baseline">正文与对白先行</option></select></label><p v-if="chosenVersion" class="chosen-method">本任务下一轮已选用：方法 v{{ chosenVersion.version }} <button class="text-link" @click="chosenVersionId = ''">取消本次手选</button></p></details>
          </details>
          <StorySkillPanel class="creator-skills" v-model="skillSelections" v-model:apply-project-skills="applyProjectSkills" v-model:auto-summarize="autoSummarizeSkill" :project="project" :run-id="run?.run.id ?? ''" :summary-job-id="run?.skill_summary_job_id" :disabled="busy || inFlight" />
          <label>经历复用<select v-model="memoryMode" :disabled="busy || inFlight || Boolean(run)"><option value="existing">使用当前方法</option><option value="routed">自动挑选本项目经历</option></select></label>
          <p v-if="memoryMode === 'routed'" class="hint">手选 Skill 保留；经验选择与整理会增加模型调用。</p>
          <div class="primary-actions"><button v-if="!task" class="primary" :disabled="busy || !brief.trim() || (Boolean(narrativeProjectId) && !narrativeReady)" @click="createAndProduce">生成草稿 <span>→</span></button><button v-else class="primary" :disabled="busy || inFlight || (Boolean(narrativeProjectId) && !narrativeReady)" @click="perform(startProduction)">重新生成</button><p v-if="narrativeProjectId && !narrativeReady" class="hint">请先保存章节、场景和资料引用。</p></div>
        </section>

        <details class="card project-drawer" :open="projectDrawerOpen" @toggle="projectDrawerOpen = ($event.target as HTMLDetailsElement).open"><summary>章节、场景与路线检查</summary><StoryProjectPanel ref="projectPanel" :workspace="project" :selected-project-id="narrativeProjectId" :selected-scene-id="narrativeSceneId" :artifact-source="contentSource" :can-bind="canAnchor && !inFlight" :locked="busy || inFlight" @select="chooseNarrativeProject" @changed="refreshCatalogs" />
        <GeneratedStoryToolPanel :workspace="project" :project-id="narrativeProjectId" :locked="busy || inFlight || !narrativeReady" /></details>

        <section v-if="task" id="creator-production" class="card production">
          <header class="section-title"><div><span class="eyebrow">02 / WORKING TOGETHER</span><h2>{{ gate ? `确认${gateSubject}` : '生成进度' }}</h2></div><div class="compact-actions"><button :disabled="busy" @click="perform(poll)">刷新</button><button v-if="run?.collaboration?.can_pause && !run.collaboration.pause_requested" :disabled="busy" @click="pause">在下一步前暂停</button><button v-if="run && (isRunning || gate)" :disabled="busy" @click="gate ? resume('cancel') : perform(async () => { await api.cancel(run!.run.id); await poll(); })">停止本轮</button></div></header>
          <StoryRunSummary :project="project" :task-id="task.id" :task-origin="task.origin_kind" :draft-revision="task.revision" :run="isPlanning ? null : run" :events="events" :planning="isPlanning" :gate-subject="gateSubject" :impact="applicationDetail?.impact" />
          <details v-if="graph" class="execution-details"><summary>生成步骤与指导</summary><ol class="production-steps"><li v-for="(node, index) in graph.nodes" :key="node.id" :class="{ active: nodeStatus(node.id) === 'running', waiting: gate?.node_id === node.id }"><span class="step-number">{{ index + 1 }}</span><div><strong>{{ operation(node.operation) }}</strong><span>{{ creatorStatus(nodeStatus(node.id)) }}</span><details v-if="!SYSTEM_CREATOR_CHECKS.has(node.operation)"><summary>{{ gate ? '查看或调整这一步指导' : '本步如何工作' }}</summary><label v-if="gate">步骤指导（选填）<textarea v-model="guidance[node.id]" rows="3" maxlength="8000" /></label><p v-else class="hint">{{ node.guidance || '没有额外方法指导。' }}</p></details></div></li></ol></details>
          <details v-if="priorStageDraft"><summary>上次未提交的意见（旧版本或停点）</summary><p>{{ priorStageDraft.feedback }}</p><p>{{ priorStageDraft.gateNotes }}</p><pre>{{ raw(priorStageDraft.guidance) }}</pre></details>
          <section v-if="gate" id="creator-author-turn" class="author-turn">
            <div class="turn-badge">待确认</div><h3>{{ gateSubject }}</h3>
            <article v-if="gateSubject === '故事大纲' && storyPlan" class="planning-copy"><h4>{{ storyPlan.premise }}</h4><p v-if="storyPlan.conflict">核心冲突：{{ storyPlan.conflict }}</p><ol><li v-for="(beat, index) in storyPlan.beats" :key="index">{{ beat }}</li></ol><ul v-if="storyPlan.choices?.length"><li v-for="choice in storyPlan.choices" :key="choice">{{ choice }}</li></ul></article>
            <article v-else-if="gateSubject === '选择与后果' && quest?.logic" class="planning-copy"><p v-for="(branch, index) in gateChoices" :key="branch.id"><strong>分支 {{ index + 1 }}</strong> · {{ branch.outcome || quest.stages?.find(stage => stage.id === branch.to_stage)?.summary || '继续下一阶段' }}<span v-if="branch.effects.length"> · {{ branch.effects.map(effect => `${quest?.logic?.variables.find(variable => variable.id === effect.var)?.name || effect.var} ${effect.op === 'inc' ? '+' : effect.op === 'dec' ? '−' : '→'} ${String(effect.value)}`).join('；') }}</span></p><p v-if="!gateChoices.length">当前场景未定义分支后果。</p></article>
            <article v-else-if="gateSubject === '角色对白' && tree" class="planning-copy"><p v-for="node in tree.nodes" :key="node.id">{{ node.text }}</p></article>
            <article v-else-if="quest" class="planning-copy"><h4>{{ quest.title }}</h4><p>{{ quest.objective }}</p><ol><li v-for="stage in quest.stages" :key="stage.id">{{ stage.summary }}</li></ol></article>
            <p v-else>当前暂无可展示的草稿，请展开生成步骤查看停点。</p>
            <p v-if="gate.permission_required" class="failure-copy">这一步未获准执行。请调整后续约定，或停止本轮。</p>
            <details><summary>调整后续约定</summary><label>参与方式<select v-model="gatePolicy.mode"><option v-for="mode in modePresets" :key="mode.id" :value="mode.id">{{ mode.name }}</option></select></label><div class="policy-toggles"><label class="check"><input v-model="gatePolicy.plan_review" type="checkbox">生成场景前确认大纲</label><label class="check"><input v-model="gatePolicy.key_step_review" type="checkbox">关键步骤开始前确认</label><label class="check"><input v-model="gatePolicy.allow_read_tools" type="checkbox">允许工具读取所选项目资料</label><label class="check"><input v-model="gatePolicy.allow_candidate_trial" type="checkbox">允许候选流程试用</label></div><StoryStageSettings v-model="gatePolicy" :operations="graph?.nodes.map(node => ({ operation: node.operation }))" :disabled="busy" /><p v-if="policyChanged" class="hint">提交后按新约定另开一轮，旧运行和已确认记录保留。</p></details>
            <details v-if="run?.collaboration?.stage_agreements?.length"><summary>本轮实际约定</summary><section v-for="stage in run.collaboration.stage_agreements" :key="stage.id"><h4>{{ stage.title }}</h4><p v-for="item in stage.operations" :key="item.operation" class="hint">{{ item.title }} · {{ item.system_required ? '系统执行' : item.allowed ? '允许' : '不允许' }}{{ item.review_before ? ' · 执行前确认' : '' }}</p></section></details>
            <label>修改意见（选填）<textarea v-model="gateNotes" rows="3" /></label><label v-if="gateNotes.trim()">修改步骤<select v-model="gateTarget"><option v-for="node in creativeNodes" :key="node.id" :value="node.id">{{ operation(node.operation) }}</option></select></label><div class="primary-actions"><button class="primary" :disabled="busy || !run?.collaboration?.can_resume || gate.permission_required || Boolean(gateNotes.trim()) || Boolean(Object.keys(changedGuidance).length) || policyChanged" @click="resume('continue')">{{ gateSubject === '故事大纲' ? '按此大纲继续' : '继续生成' }}</button><button :disabled="busy || !run?.collaboration?.can_resume || (!gateNotes.trim() && !Object.keys(changedGuidance).length && !policyChanged)" @click="resume('revise')">按调整继续</button></div>
          </section>
          <p v-if="run?.run.error || run?.error || job?.error" class="failure-copy">{{ run?.error || run?.run.error || job?.error }}</p>
        </section>

        <section v-if="run" id="creator-manuscript" class="card manuscript">
          <header class="section-title"><div><span class="eyebrow">03 / STORY & CHOICES</span><h2>{{ quest?.title || '故事草稿' }}</h2></div><div class="tab-switch"><button :class="{ selected: productTab === 'read' }" @click="productTab = 'read'">阅读正文</button><button :class="{ selected: productTab === 'branches' }" :disabled="!tree" @click="productTab = 'branches'">查看分支</button></div></header>
          <section v-if="applicationDetail" id="creator-last-impact" class="application-handoff"><h3>本次修改已保存</h3><p class="hint">当前稿 v{{ applicationDetail.receipt.revision }}<span v-if="savedPreviewBeforeEdit"> · 原试玩保留在第 {{ savedPreviewBeforeEdit.view.action_seq }} 步</span></p><StoryImpactSummary :impact="applicationDetail.impact" /><section v-if="readingTarget" class="reading-return"><h4>修改后的原位置</h4><p>{{ readingTarget.text }}</p><button class="text-link" @click="showReadingTarget">在正文中查看</button></section><div class="compact-actions"><button :disabled="busy || !canPreview" @click="startPreview">在新稿试玩</button><button v-if="savedPreviewBeforeEdit" :disabled="busy || Boolean(pendingTaskPreview)" @click="continueEdited">在新稿继续原位置</button><button v-if="savedPreviewBeforeEdit" :disabled="busy || Boolean(pendingTaskPreview)" @click="continueOriginalTask">沿旧版继续</button><button v-if="applicationDetail.receipt.continuation?.narrative_project_id" :disabled="busy" @click="openApplicationProject">打开原场景</button></div><details><summary>修改记录</summary><pre>{{ raw(applicationDetail.receipt) }}</pre></details></section>
          <p v-if="appliedArtifact" class="hint">当前草稿 v{{ appliedArtifact.revision }} · 生成记录对应 v{{ run.run.revision }}。</p><p v-if="!quest && !tree && !storyPlan" class="empty-copy">草稿生成后显示在这里。</p>
          <div class="reading-grid"><div class="manuscript-body"><template v-if="productTab === 'read'"><details v-if="quest" class="outline-notes"><summary>场景梗概</summary><article class="quest-document"><span class="document-label">场景目标</span><p :id="readingId('/quest/objective')">{{ quest.objective }}</p><button v-if="canAnchor" class="text-link" @click="anchor('/quest/objective', quest.objective)">修改这段目标</button><section v-for="(stage, index) in quest.stages" :key="stage.id"><h3><span>{{ String(index + 1).padStart(2, '0') }}</span>故事阶段</h3><p :id="readingId(`/quest/stages/${index}/summary`)">{{ stage.summary }}</p><button v-if="canAnchor" class="text-link" @click="anchor(`/quest/stages/${index}/summary`, stage.summary)">针对这一阶段反馈</button></section></article></details><article v-if="!quest && storyPlan" class="quest-document"><span class="document-label">故事大纲</span><p>{{ storyPlan.premise }}</p><ol><li v-for="beat in storyPlan.beats" :key="beat">{{ beat }}</li></ol></article><details v-if="tree" class="dialogue-document" open><summary>角色对白</summary><article v-for="(node, key) in tree.nodes" :key="key"><span v-if="node.speaker_id" class="speaker">{{ entityName(node.speaker_id) }}</span><p :id="readingId(`/tree/nodes/${pointer(key)}/text`)">{{ node.text }}</p><ul v-if="node.choices?.length"><li v-for="(choice, index) in node.choices" :key="index" :id="readingId(`/tree/nodes/${pointer(key)}/choices/${index}/text`)">{{ choice.text }} <button v-if="canAnchor" class="text-link" @click="anchor(`/tree/nodes/${pointer(key)}/choices/${index}/text`, choice.text)">改这个选择</button></li></ul><button v-if="canAnchor" class="text-link" @click="anchor(`/tree/nodes/${pointer(key)}/text`, node.text)">修改这段对白</button></article></details></template><template v-else-if="tree"><StoryBranchMap :tree="tree" :selected="selectedNode" :visited="visitedNodes" @select="selectedNode = $event" /><button v-if="currentDialogue && canAnchor" class="text-link" @click="anchor(`/tree/nodes/${pointer(selectedNode)}/text`, currentDialogue.text)">针对当前查看位置反馈</button></template></div><aside class="play-column"><p v-if="pendingTaskPreview" class="hint">新会话已建立，尚未读回。<button :disabled="busy" @click="perform(readPendingTaskPreview)">读取试玩会话</button></p><button v-if="preview && !canFeedbackAtPreview && !taskPreviewIsHistorical && !savedPreviewBeforeEdit" :disabled="busy || Boolean(pendingTaskPreview)" @click="continueOriginalTask">沿此旧版继续</button><p v-if="taskPreviewIsHistorical" class="hint">沿旧版隔离试玩，当前稿保持不变。</p><p v-else-if="preview && !canFeedbackAtPreview" class="hint">此试玩使用修改前的稿件。</p><StoryPlaytest :preview="preview" :quest="previewQuest" :tree="previewTree" :feedback-enabled="canFeedbackAtPreview" :step-disabled="Boolean(pendingTaskPreview) || Boolean(preview && !canFeedbackAtPreview && !taskPreviewIsHistorical)" :busy="busy" :can-start="canPreview" @start="startPreview" @step="step" @feedback="feedbackAtPreview" /><p v-if="gate" class="hint">确认并完成生成后可试玩。</p><p v-else-if="!canPreview && bound" class="hint">当前草稿版本已变更或尚未准备好，请重新生成后试玩。</p></aside></div>
          <details v-if="requirementChecks.length" class="requirements-review" :open="requirementChecks.some(check => check.status === 'failed')"><summary>检查结果 · {{ requirementChecks.filter(check => check.status === 'failed').length }}项未通过 · {{ requirementChecks.filter(check => ['unknown', 'withheld'].includes(check.status)).length }}项需确认</summary><ul><li v-for="(check, index) in requirementChecks" :key="index"><span :class="['check-state', check.status]">{{ creatorStatus(check.status, "requirements") }}</span><p>{{ check.reason }}<small v-if="check.scope === 'free_text'">请结合草稿确认这项创作要求。</small></p></li></ul></details>
          <section class="feedback-box"><h3>修改意见</h3><p v-if="feedbackAnchor" class="quoted-anchor">已定位：“{{ anchoredText }}” <button class="text-link" @click="feedbackAnchor = null; anchoredText = ''">取消定位</button></p><label>修改内容（必填）<textarea v-model="feedback" rows="3" /></label><details class="field-help"><summary>填写示例</summary><p class="hint">把追问的语气改得含蓄些，保留人物动机。</p></details><div class="feedback-controls"><label>这次改变<select v-model="feedbackKind"><option value="content">当前故事内容</option><option value="workflow">当前制作方式</option></select></label><label>相关制作步骤<select v-model="feedbackTarget" :disabled="Boolean(feedbackAnchor) && feedbackKind === 'content'"><option value="">由当前范围定位</option><option v-for="node in creativeNodes" :key="node.id" :value="node.operation">{{ operation(node.operation) }}</option></select></label></div><label v-if="currentTaskPreview && projectEditContext.project_preview_id">修改后继续的试玩<select v-model="continuationMode"><option value="task">当前草稿试玩</option><option value="project">章节路线试玩</option></select></label><StoryPatchCard v-if="task && (!applicationDetail || Boolean(patchSelection))" :project="project" :task-id="task.id" :revision="task.revision" :source="contentSource" :selection="patchSelection" :goal="feedback" :actor-kind="actorKind" :continuation="continuationContext" :locked="busy || isRunning || isPlanning || Boolean(gate)" @working="patchWorking = $event" @applied="patchApplied" /><button class="primary" :disabled="busy || inFlight || !feedback.trim()" @click="reviseFromFeedback">按意见重新生成</button></section>
        </section>

        <details class="card methods"><summary>高级：工作图与流程版本</summary>
          <div class="method-toolbar"><label>流程<select v-model="familyId" @change="changeFamily"><option value="">暂不手选</option><option v-for="item in families" :key="item.id" :value="item.id">{{ item.name }}</option></select></label><label v-if="family">版本<select v-model="versionId"><option v-for="item in family.versions" :key="item.id" :value="item.id">v{{ item.version }}{{ family.default_version_id === item.id ? ' · 后续默认' : '' }} · {{ creatorStatus(item.state.author_acceptance, 'author') }}</option></select></label></div>
          <template v-if="family && version"><div class="method-states"><span>运行检查：{{ creatorStatus(version.state.technical_status, "review") }}</span><span>试用：{{ creatorStatus(version.state.trial_status, "trial") }}</span><span>采用情况：{{ creatorStatus(version.state.author_acceptance, "author") }}</span><span>效果比较：{{ creatorStatus(version.state.improvement_status, "comparison") }}</span></div><h3>版本改动</h3><ul class="method-diff"><li v-for="change in methodDiff" :key="change">{{ change }}</li></ul><details><summary>保留的方法指导</summary><ul><li v-for="(entry, index) in version.entries ?? []" :key="index">{{ typeof entry.text === 'string' ? entry.text : '未填写指导内容。' }}</li></ul></details><StoryMethodTrial v-if="trialRequirements" :project="project" :task="task" :version="version ?? null" :family-hash="family.family_hash" :requirements="trialRequirements" :disabled="busy || patchWorking" :execution-active="inFlight" @selection="useTrial" @changed="refreshCatalogs" /><p v-else class="hint">请先修正草稿要求，再设置本任务试用。</p><StoryMethodOrigin :project="project" :family-id="family.id" :version-id="version.id" /><details><summary>适用范围与候选依据</summary><p class="hint">适用范围来自当前版本的保存条件；内容与协作效果尚需实际使用确认。</p><pre>{{ raw({ conditions: version.conditions, proposal: version.delta?.proposal, changes: version.delta?.graph_compilation, parent_version_id: version.parent_version_id }) }}</pre></details><div class="compact-actions"><button v-if="version.state.author_acceptance === 'accepted'" :disabled="busy || inFlight || family.default_version_id !== version.id" @click="selectedTrial = null; chosenVersionId = version.id; notice = '已选为本任务下一次生成的流程。'">本任务使用此版</button><button class="primary" :disabled="busy" @click="methodAction('accept')">采用并设为默认</button><button :disabled="busy" @click="methodAction('reject')">不采用这版</button><button :disabled="busy || family.default_version_id === version.id" @click="methodAction('rollback')">后续任务退回此版</button></div><p class="hint">设为默认或退回此版，将影响之后新建的任务。</p></template>
          <p v-else class="hint empty-method">尚未选中流程。生成草稿后可将本次流程保存为候选。</p><div v-if="run" class="save-method"><label>流程名称（必填）<input v-model="skillName" maxlength="200"></label><button :disabled="busy || inFlight || !skillName.trim()" @click="saveMethod">保存流程候选</button></div><p v-if="methodJob" class="hint">流程改进：{{ creatorStatus(methodJob.status) }}{{ methodJob.error ? ` · ${methodJob.error}` : '' }}</p>
        </details>

        <details v-if="run" class="debug-drawer" @toggle="loadDebug"><summary>运行详情</summary><div class="compact-actions"><router-link class="quiet-link" to="/evolution">打开流程调试台 ↗</router-link><button :disabled="busy" @click="downloadEvidence">导出运行记录</button></div><details><summary>本轮策略与执行事件</summary><pre>{{ raw({ policy: run.collaboration, method_decision: run.method_decision, selection_decision_id: run.selection_decision_id, events: events.slice(-80) }) }}</pre></details><details v-for="request in requests" :key="request.id"><summary>{{ operation(graph?.nodes.find((node) => node.id === request.node_id)?.operation ?? request.node_id) }} · 第{{ request.attempt }}次派发</summary><pre>{{ raw({ context: request.context_manifest, request: request.request, request_hash: request.request_hash }) }}</pre></details></details>
      </main>
    </div>
  </div>
</template>

<style scoped>
.creator { max-width: 1510px; margin: 0 auto; --creator-teal: #8bc5b9; }
.workspace { display: grid; grid-template-columns: 210px minmax(0,1fr); gap: 1.4rem; align-items: start; }
.project-rail { position: sticky; top: 1rem; border: 1px solid var(--ow-paper-edge); border-radius: 9px; background: var(--ow-panel-2); padding: .9rem; }
.rail-title { display: flex; justify-content: space-between; align-items: center; font-size: .8rem; letter-spacing: .05em; color: var(--ow-gold); margin-bottom: .8rem; }.rail-title button { border: 0; font-size: 1.2rem; padding: .1rem .3rem; }
.task-list { max-height: 45vh; overflow: auto; display: grid; gap: .3rem; margin-top: .55rem; }.task-list button,.new-story { text-align: left; width: 100%; font-size: .78rem; line-height: 1.65; padding: .7rem; border-color: transparent; }.task-list small { display: block; font-size: .65rem; color: var(--ow-ink-mute); margin-top: .3rem; }.task-list .selected,.new-story.selected { background: var(--ow-gold-faint); border-color: var(--ow-gold-soft); }.rail-note { color: var(--ow-ink-mute); font-size: .68rem; line-height: 1.9; margin: 1rem .2rem .1rem; }.rail-refresh { font-size: .7rem; margin-top: .6rem; width: 100%; }
.creator-main { display: grid; gap: 1.2rem; min-width: 0; }.card { border: 1px solid var(--ow-paper-edge); border-radius: 12px; background: linear-gradient(135deg,rgba(45,34,49,.9),rgba(24,24,42,.92)); padding: 1.5rem; min-width: 0; box-shadow: 0 7px 28px rgba(0,0,0,.1); }
.section-title { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: .9rem; margin-bottom: 1.2rem; }.eyebrow { display: block; font-size: .64rem; color: var(--ow-gold); letter-spacing: .16em; margin-bottom: .4rem; }h2 { margin: 0; font-size: 1.25rem; font-weight: 600; }h3 { font-size: .95rem; margin: 1rem 0 .6rem; }h4 { font-size: 1rem; font-weight: 500; }.subtle-badge { font-size: .7rem; color: var(--ow-ink-mute); border: 1px solid var(--ow-paper-edge); padding: .3rem .55rem; border-radius: 5px; }
button,input,select,textarea { font: inherit; color: inherit; }button { background: rgba(255,255,255,.02); border: 1px solid var(--ow-paper-edge); border-radius: 6px; padding: .6rem .85rem; font-size: .8rem; cursor: pointer; }button:hover:not(:disabled) { border-color: var(--ow-gold); background: var(--ow-gold-faint); }button:disabled { opacity: .42; cursor: default; }.primary { background: var(--ow-gold); color: var(--ow-night-deep); border-color: var(--ow-gold); font-weight: 600; padding: .75rem 1rem; }.primary:hover:not(:disabled) { background: var(--ow-gold-bright); }.primary span { margin-left: 1.4rem; }.quiet-link { color: var(--creator-teal); font-size: .76rem; text-decoration: none; }
label { display: grid; gap: .45rem; font-size: .8rem; color: var(--ow-ink-soft); margin: .65rem 0; }input,select,textarea { background: rgba(12,12,25,.3); border: 1px solid var(--ow-paper-edge); border-radius: 6px; padding: .65rem .7rem; min-width: 0; width: 100%; }textarea { resize: vertical; line-height: 1.8; }input:focus,select:focus,textarea:focus { outline: 2px solid var(--ow-gold-soft); outline-offset: 1px; }.large-label { font-size: 1.1rem; font-family: var(--ow-serif); margin-bottom: .8rem; }.idea-input { font-family: var(--ow-serif); font-size: 1.05rem; line-height: 1.95; }.hint { color: var(--ow-ink-mute); font-size: .76rem; line-height: 1.85; margin: .55rem 0; }.brief-snapshot { font-family: var(--ow-serif); white-space: pre-wrap; line-height: 2; font-size: 1rem; padding-bottom: .7rem; }.setup-details { margin-top: 1rem; }.setup-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1.6rem; margin-top: .8rem; }.setup-grid > section { min-width: 0; }.check { display: flex; align-items: flex-start; gap: .55rem; line-height: 1.7; }.check input { width: auto; margin-top: .3rem; flex: none; }.check small { color: var(--ow-ink-mute); font-size: .68rem; }.source-item small { display: block; }.source-list,.entity-list { max-height: 14rem; overflow: auto; }.source-item { padding: .45rem .1rem; border-bottom: 1px solid rgba(255,255,255,.04); }.add-source { margin: .9rem 0; }.add-source > label { margin-top: .7rem; }.count-fields { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: .6rem; }.role-row { display: flex; gap: .4rem; margin: .5rem 0; }.role-row label { flex: 1; margin: 0; } .role-row label:first-child { flex: 1.3; }.role-row select { flex: 1; }.role-row button { padding: .3rem .55rem; }
summary { font-size: .8rem; cursor: pointer; color: var(--ow-ink-soft); line-height: 1.8; }details { margin: .6rem 0; }details > summary { padding: .2rem 0; }.collaboration-setup { border-top: 1px solid var(--ow-paper-edge); margin-top: 1.2rem; padding-top: .4rem; }.mode-cards { display: grid; grid-template-columns: repeat(3,minmax(0,1fr)); gap: .7rem; }.mode-cards button { position: relative; text-align: left; display: grid; gap: .45rem; padding: 1rem; line-height: 1.65; }.mode-cards strong { font-size: .88rem; }.mode-cards button > span:not(.radio-mark) { color: var(--ow-ink-soft); font-size: .75rem; }.mode-cards small { color: var(--ow-ink-mute); font-size: .7rem; }.mode-cards .selected { border-color: var(--creator-teal); background: rgba(139,197,185,.07); }.radio-mark { color: var(--creator-teal); font-size: .65rem; }.policy-toggles { display: grid; grid-template-columns: 1fr 1fr; gap: .2rem 1rem; }.primary-actions { display: flex; flex-wrap: wrap; gap: .7rem 1rem; align-items: center; margin-top: 1rem; }.primary-actions .hint { flex: 1; min-width: 12rem; }.message { border-radius: 8px; padding: .8rem 1rem; margin: 0 0 1rem; font-size: .85rem; line-height: 1.8; }.message.error { color: #f1b3a5; background: rgba(200,85,61,.12); border: 1px solid rgba(200,85,61,.3); }.message.notice { color: #bcdac6; background: rgba(93,149,116,.11); border: 1px solid rgba(93,149,116,.28); }
.compact-actions { display: flex; flex-wrap: wrap; gap: .55rem; align-items: center; }.compact-actions button { font-size: .73rem; }.run-summary { display: flex; flex-wrap: wrap; align-items: center; gap: .8rem; color: var(--ow-ink-mute); font-size: .76rem; }.run-summary strong { color: var(--ow-ink); font-size: .83rem; }.activity-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--ow-ink-mute); }.activity-dot.live { background: var(--creator-teal); box-shadow: 0 0 0 4px rgba(139,197,185,.1); }.notice-inline { font-size: .8rem; color: var(--ow-gold); line-height: 1.8; }.production-steps { list-style: none; display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: .6rem; padding: 0; margin: 1.1rem 0; }.production-steps > li { display: flex; gap: .7rem; border: 1px solid rgba(255,255,255,.07); padding: .8rem; border-radius: 7px; }.step-number { color: var(--ow-gold); font: .75rem var(--ow-num); padding-top: .1rem; }.production-steps > li > div { flex: 1; min-width: 0; }.production-steps strong { display: block; font-size: .79rem; line-height: 1.6; }.production-steps strong + span { color: var(--ow-ink-mute); font-size: .68rem; }.production-steps .active { border-color: rgba(139,197,185,.5); }.production-steps .waiting { border-color: var(--ow-gold); background: var(--ow-gold-faint); }.production-steps details { margin-bottom: 0; }.production-steps summary { font-size: .68rem; }.author-turn { margin-top: 1rem; padding: 1.2rem; border: 1px solid var(--ow-gold-soft); border-radius: 9px; background: var(--ow-gold-faint); }.turn-badge { color: var(--ow-gold); font-size: .7rem; letter-spacing: .12em; }.author-turn > p { color: var(--ow-ink-soft); font-size: .85rem; line-height: 1.8; }.planning-copy { border-top: 1px solid var(--ow-paper-edge); margin-top: 1rem; font-family: var(--ow-serif); line-height: 1.9; }.planning-copy li { margin: .4rem 0; }.failure-copy { color: #e7a89a; line-height: 1.8; font-size: .8rem; }
.reading-return { margin: .8rem 0; }.reading-return p { white-space: pre-wrap; font-family: var(--ow-serif); font-size: 1rem; line-height: 1.9; }.application-handoff { margin-bottom: 1rem; }.tab-switch { display: flex; gap: .3rem; }.tab-switch .selected { color: var(--ow-gold); background: var(--ow-gold-faint); border-color: var(--ow-gold-soft); }.reading-grid { display: grid; grid-template-columns: minmax(0,1.4fr) minmax(270px,1fr); gap: 1.5rem; }.manuscript-body { min-width: 0; }.quest-document { font-family: var(--ow-serif); line-height: 2; }.quest-document p { white-space: pre-wrap; margin: .5rem 0; font-size: 1rem; }.document-label { color: var(--ow-gold); font: .7rem var(--ow-display); letter-spacing: .12em; }.quest-document section { border-top: 1px solid var(--ow-paper-edge); margin-top: 1.3rem; padding-top: .2rem; }.quest-document h3 { font-family: var(--ow-display); font-size: .75rem; color: var(--ow-ink-mute); }.quest-document h3 span { font-family: var(--ow-num); color: var(--ow-gold); margin-right: .6rem; }.play-column { border-left: 1px solid var(--ow-paper-edge); padding-left: 1.4rem; min-width: 0; }.text-link { padding: .15rem 0; border: 0; background: transparent; color: var(--creator-teal); font-family: var(--ow-display); font-size: .72rem; }.dialogue-document { margin-top: 1.5rem; }.dialogue-document > article { border-bottom: 1px solid rgba(255,255,255,.06); padding: 1rem 0; }.speaker { color: var(--ow-gold); font-size: .75rem; }.dialogue-document p { font-family: var(--ow-serif); line-height: 1.95; white-space: pre-wrap; margin: .45rem 0; }.dialogue-document ul { color: var(--ow-ink-soft); font-size: .85rem; line-height: 1.8; padding-left: 1.1rem; }.empty-copy { color: var(--ow-ink-mute); line-height: 1.9; padding: 1rem 0; font-size: .85rem; }.requirements-review { margin-top: 1.5rem; border-top: 1px solid var(--ow-paper-edge); padding-top: .5rem; }.requirements-review ul { list-style: none; padding: 0; }.requirements-review li { display: flex; align-items: flex-start; gap: .8rem; padding: .55rem 0; border-bottom: 1px solid rgba(255,255,255,.05); }.check-state { font-size: .65rem; padding: .2rem .4rem; color: var(--ow-ink-mute); border: 1px solid var(--ow-paper-edge); border-radius: 4px; flex: none; }.check-state.passed { color: var(--creator-teal); }.check-state.failed { color: #e7a89a; }.requirements-review li p { font-size: .78rem; line-height: 1.8; margin: 0; }.requirements-review small { display: block; color: var(--ow-ink-mute); font-size: .68rem; }.feedback-box { margin-top: 1.3rem; border-top: 1px solid var(--ow-paper-edge); padding-top: .6rem; }.feedback-controls { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }.quoted-anchor { border-left: 2px solid var(--creator-teal); padding: .4rem .7rem; font-size: .82rem; line-height: 1.9; background: rgba(139,197,185,.05); }
.creator-skills { border-top: 1px solid var(--ow-paper-edge); margin-top: 1.3rem; padding-top: 1.1rem; }
.method-toolbar,.save-method { display: flex; gap: .8rem; flex-wrap: wrap; align-items: flex-end; }.method-toolbar label { min-width: 12rem; flex: 1; }.method-states { display: flex; flex-wrap: wrap; gap: .4rem; margin: 1rem 0; }.method-states span { color: var(--ow-ink-soft); border: 1px solid var(--ow-paper-edge); border-radius: 5px; padding: .4rem .5rem; font-size: .68rem; }.method-diff { color: var(--ow-ink-soft); font-size: .82rem; line-height: 1.9; padding-left: 1.1rem; }.methods details li { color: var(--ow-ink-mute); font-size: .78rem; line-height: 1.8; }.save-method { border-top: 1px solid var(--ow-paper-edge); padding-top: .7rem; margin-top: 1rem; }.save-method label { flex: 1; }.save-method button { margin-bottom: .65rem; }.debug-drawer { border: 1px solid var(--ow-line); border-radius: 8px; padding: .8rem 1rem; background: var(--ow-panel-2); }.debug-drawer > summary { color: var(--ow-ink-mute); }.debug-drawer .compact-actions { margin-top: .8rem; }pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 25rem; overflow: auto; background: rgba(0,0,0,.15); padding: .8rem; font-size: .7rem; line-height: 1.7; border-radius: 6px; }
@media (max-width: 1150px) { .workspace { grid-template-columns: 170px minmax(0,1fr); gap: 1rem; }.reading-grid { grid-template-columns: minmax(0,1fr); }.play-column { border-left: 0; padding-left: 0; border-top: 1px solid var(--ow-paper-edge); padding-top: 1.2rem; }.mode-cards { grid-template-columns: minmax(0,1fr); }.mode-cards button { grid-template-columns: 1rem 1fr; gap: .2rem .6rem; }.mode-cards small,.mode-cards button > span:not(.radio-mark) { grid-column: 2; }.card { padding: 1.2rem; } }
@media (max-width: 780px) { .workspace { grid-template-columns: minmax(0,1fr); }.project-rail { position: static; }.task-list { max-height: 9rem; }.rail-note { display: none; }.setup-grid,.production-steps,.policy-toggles { grid-template-columns: minmax(0,1fr); }.setup-grid { gap: .5rem; }.feedback-controls { grid-template-columns: minmax(0,1fr); gap: 0; }.card { padding: 1rem; } }
</style>
