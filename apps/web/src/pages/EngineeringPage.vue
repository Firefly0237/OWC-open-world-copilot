<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import PageHead from "../components/PageHead.vue";
import {
  ApiError,
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentProject,
  humanizeError,
  llmConfig,
  llmParams,
} from "../api";
import { notifyError, pushToast } from "../toast";

type ToolKey = "quest" | "spec" | "dialogue" | "flag" | "repair" | "style" | "quality" | "history";
type CatalogKind = "quest" | "dialogue_tree" | "style_rubric";

interface StyleRubricOption {
  id: string;
  label: string;
  version: string;
  element_count: number;
}

interface EngineeringCatalog {
  project: string;
  quest_ids: string[];
  dialogue_tree_ids: string[];
  style_rubrics: StyleRubricOption[];
  kind?: CatalogKind | null;
  total?: number | null;
  next_cursor?: string | null;
}

interface AgentRunSummary {
  id: string;
  goal: string;
  stop_reason: string;
  step_count: number;
  cost_usd: number;
  latency_ms: number;
  agent_name: string;
  created_at: string;
}

interface AgentRunDetail extends AgentRunSummary {
  transcript: Record<string, unknown>;
}

interface AgentRunsResponse {
  runs: AgentRunSummary[];
  total: number;
  next_cursor: string | null;
  previous_cursor: string | null;
  page_cursor: string | null;
  snapshot_etag: string;
}

interface QualityResult {
  phase: string;
  stop_reason: string;
  export_ready: boolean;
  audit_totals: Record<string, number>;
  top_issues: Record<string, unknown>[];
  patch_proposals: {
    patch_id?: string;
    id?: string;
    issue_id?: string | null;
    rationale?: string;
    ops?: Record<string, unknown>[];
  }[];
  writes_canon: false;
  [key: string]: unknown;
}

interface ModelTelemetry {
  calls: number;
  input_tokens: number;
  output_tokens: number;
  total_cost_usd: number;
  cost_is_estimate: boolean;
}

interface CostBudget {
  budget_usd: number | null;
  used_usd: number;
  remaining_usd: number | null;
  over_budget: boolean;
  cost_is_estimate: boolean;
}

interface RepairPlanResult {
  plan: Record<string, unknown>;
  candidate_source: "deterministic_fixers" | "deterministic_plus_model";
  used_llm: boolean;
  llm_mode: "offline" | "real";
  telemetry: ModelTelemetry;
  cost_budget: CostBudget;
  writes_canon: false;
}

interface StyleSweepResult {
  rubric_label: string;
  rubric_version: string;
  scanned_total: number;
  l1_total: number;
  l2_total: number;
  dismissed_count: number;
  llm_used: boolean;
  judge_mode: "disabled" | "offline_double" | "real_model";
  judge_limit: number;
  judged_count: number;
  judge_skipped: number;
  telemetry: ModelTelemetry;
  cost_budget: CostBudget;
  hits: { ref?: string; field?: string; evidence?: string; [key: string]: unknown }[];
  review_suggested: Record<string, unknown>[];
  markdown: string;
  [key: string]: unknown;
}

interface SpecGrounding {
  surface: string;
  status: "grounded" | "entity_no_flag" | "unknown";
  flag_id: string;
  flag_name: string;
  confidence: number;
  note: string;
}

interface SpecTranslationResult {
  quest_id: string;
  translator_mode: "deterministic" | "real_model" | "real_model_fallback";
  used_llm: boolean;
  degraded: boolean;
  degradation_reasons: string[];
  telemetry: ModelTelemetry;
  cost_budget: CostBudget;
  writes_canon: false;
  outcome: {
    intent: string;
    status: "needs_confirmation" | "rejected" | "unsupported";
    draft: {
      pattern: string;
      operands: string[];
      description: string;
    };
    groundings: SpecGrounding[];
    spec: Record<string, unknown> | null;
    back_translation: string;
    message: string;
  };
}

interface SpecSubmitResult {
  quest_id: string;
  review_item_id: string;
  review_item?: Record<string, unknown>;
  writes_canon: false;
}

const TOOLS: { key: ToolKey; label: string; hint: string }[] = [
  { key: "quest", label: "任务穷举", hint: "有界搜索所有可达结局" },
  { key: "spec", label: "规约编写", hint: "翻译、接地、回译后送审" },
  { key: "dialogue", label: "对话游走", hint: "复现路径或枚举结局" },
  { key: "flag", label: "旗标引用", hint: "查声明、读者和写者" },
  { key: "repair", label: "MCTS 修复", hint: "验证候选，只生成修复计划" },
  { key: "style", label: "风格清查", hint: "按版本化 StyleRubric 扫描" },
  { key: "quality", label: "质量闭环", hint: "审计并生成待审提案" },
  { key: "history", label: "运行历史", hint: "追踪 Agent 运行记录" },
];

const active = ref<ToolKey>("quest");
const catalog = ref<EngineeringCatalog | null>(null);
const catalogLoading = ref(true);
const catalogError = ref("");
const CATALOG_PAGE_SIZE = 40;
const CATALOG_KINDS: CatalogKind[] = ["quest", "dialogue_tree", "style_rubric"];
const catalogTotals = ref<Record<CatalogKind, number>>({
  quest: 0,
  dialogue_tree: 0,
  style_rubric: 0,
});
const catalogNextCursors = ref<Record<CatalogKind, string | null>>({
  quest: null,
  dialogue_tree: null,
  style_rubric: null,
});
const catalogLoadingMore = ref<Record<CatalogKind, boolean>>({
  quest: false,
  dialogue_tree: false,
  style_rubric: false,
});
const catalogKindSequences: Record<CatalogKind, number> = {
  quest: 0,
  dialogue_tree: 0,
  style_rubric: 0,
};
let catalogLoadSequence = 0;
const llmReady = ref(llmConfig().ready);

const questId = ref("");
const questMaxStates = ref(100_000);
const questMaxDepth = ref(10_000);
const questBusy = ref(false);
const questError = ref("");
const questResult = ref<Record<string, unknown> | null>(null);

const specIntent = ref("");
const specUseModel = ref(false);
const specMaxCostUsd = ref<number | "">("");
const specBusy = ref(false);
const specError = ref("");
const specResult = ref<SpecTranslationResult | null>(null);
const specTranslatedQuestId = ref("");
const specSubmitting = ref(false);
const specSubmitError = ref("");
const specSubmitResult = ref<SpecSubmitResult | null>(null);

const dialogueId = ref("");
const dialogueMode = ref<"walk" | "explore">("walk");
const dialogueChoices = ref("");
const dialogueInitialState = ref("{}");
const dialogueMaxEndings = ref(1_000);
const dialogueBusy = ref(false);
const dialogueError = ref("");
const dialogueResult = ref<Record<string, unknown> | null>(null);

const flagName = ref("");
const flagBusy = ref(false);
const flagError = ref("");
const flagResult = ref<Record<string, unknown> | null>(null);

const repairIterations = ref(200);
const repairDepth = ref<number | "">("");
const repairSeed = ref(0);
const repairUseModel = ref(false);
const repairMaxModelCalls = ref(32);
const repairMaxCostUsd = ref<number | "">("");
const repairBusy = ref(false);
const repairError = ref("");
const repairResult = ref<RepairPlanResult | null>(null);

const rubricId = ref("");
const semanticThreshold = ref(0.5);
const styleUseJudge = ref(false);
const styleMaxJudge = ref(400);
const styleMaxCostUsd = ref<number | "">("");
const styleBusy = ref(false);
const styleError = ref("");
const styleResult = ref<StyleSweepResult | null>(null);

const proposeFixes = ref(true);
const qualityMaxIssues = ref(5);
const qualityCandidates = ref(1);
const qualityBusy = ref(false);
const qualityError = ref("");
const qualityResult = ref<QualityResult | null>(null);

const HISTORY_PAGE_SIZE = 10;
const runs = ref<AgentRunSummary[]>([]);
const runTotal = ref(0);
const runPage = ref(1);
const currentRunCursor = ref("");
const nextRunCursor = ref<string | null>(null);
const previousRunCursor = ref<string | null>(null);
const runSnapshot = ref("");
const runCursorTrail = ref<string[]>([""]);
const historyLoading = ref(false);
const historyError = ref("");
const selectedRun = ref<AgentRunDetail | null>(null);
const detailLoading = ref(false);
let historyLoadSequence = 0;

const prefix = computed(
  () => `/projects/${encodeURIComponent(currentProject())}/engineering`,
);

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}

function errorMessage(error: unknown): string {
  const message = humanizeError(error);
  notifyError(error);
  return message;
}

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
  if (!llmReady.value) {
    repairUseModel.value = false;
    styleUseJudge.value = false;
    specUseModel.value = false;
  }
}

function asObject(text: string): Record<string, boolean | number | string> {
  const parsed: unknown = JSON.parse(text || "{}");
  if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") {
    throw new Error("初始状态必须是 JSON 对象，例如 {\"gate_open\": true}。");
  }
  for (const value of Object.values(parsed)) {
    if (!["boolean", "number", "string"].includes(typeof value)) {
      throw new Error("初始状态的值只能是布尔值、数字或字符串。");
    }
  }
  return parsed as Record<string, boolean | number | string>;
}

async function loadCatalog(): Promise<void> {
  const sequence = ++catalogLoadSequence;
  for (const kind of CATALOG_KINDS) catalogKindSequences[kind] += 1;
  catalogLoadingMore.value = {
    quest: false,
    dialogue_tree: false,
    style_rubric: false,
  };
  catalogLoading.value = true;
  catalogError.value = "";
  try {
    const pages = await Promise.all(
      CATALOG_KINDS.map((kind) => apiGet<EngineeringCatalog>(
        `${prefix.value}/catalog?kind=${kind}&limit=${CATALOG_PAGE_SIZE}`,
      )),
    );
    if (sequence !== catalogLoadSequence) return;
    const byKind = Object.fromEntries(
      pages.map((page) => [page.kind, page]),
    ) as Partial<Record<CatalogKind, EngineeringCatalog>>;
    const questPage = byKind.quest;
    const dialoguePage = byKind.dialogue_tree;
    const rubricPage = byKind.style_rubric;
    if (!questPage || !dialoguePage || !rubricPage) {
      throw new Error("工程目录返回了不完整的分页结果，请重试。");
    }
    catalog.value = {
      project: questPage.project,
      quest_ids: questPage.quest_ids,
      dialogue_tree_ids: dialoguePage.dialogue_tree_ids,
      style_rubrics: rubricPage.style_rubrics,
    };
    catalogTotals.value = {
      quest: questPage.total ?? questPage.quest_ids.length,
      dialogue_tree: dialoguePage.total ?? dialoguePage.dialogue_tree_ids.length,
      style_rubric: rubricPage.total ?? rubricPage.style_rubrics.length,
    };
    catalogNextCursors.value = {
      quest: questPage.next_cursor ?? null,
      dialogue_tree: dialoguePage.next_cursor ?? null,
      style_rubric: rubricPage.next_cursor ?? null,
    };
    if (!questId.value) questId.value = questPage.quest_ids[0] ?? "";
    if (!dialogueId.value) dialogueId.value = dialoguePage.dialogue_tree_ids[0] ?? "";
    if (!rubricId.value) rubricId.value = rubricPage.style_rubrics[0]?.id ?? "";
  } catch (error) {
    if (sequence !== catalogLoadSequence) return;
    catalogError.value = errorMessage(error);
  } finally {
    if (sequence === catalogLoadSequence) catalogLoading.value = false;
  }
}

function catalogLoaded(kind: CatalogKind): number {
  if (!catalog.value) return 0;
  if (kind === "quest") return catalog.value.quest_ids.length;
  if (kind === "dialogue_tree") return catalog.value.dialogue_tree_ids.length;
  return catalog.value.style_rubrics.length;
}

async function loadMoreCatalog(kind: CatalogKind): Promise<void> {
  const cursor = catalogNextCursors.value[kind];
  if (!cursor || catalogLoadingMore.value[kind]) return;
  const sequence = ++catalogKindSequences[kind];
  catalogLoadingMore.value = { ...catalogLoadingMore.value, [kind]: true };
  try {
    const query = new URLSearchParams({
      kind,
      limit: String(CATALOG_PAGE_SIZE),
      cursor,
    });
    const page = await apiGet<EngineeringCatalog>(`${prefix.value}/catalog?${query.toString()}`);
    if (sequence !== catalogKindSequences[kind] || !catalog.value) return;
    if (kind === "quest") {
      catalog.value.quest_ids = [...catalog.value.quest_ids, ...page.quest_ids];
    } else if (kind === "dialogue_tree") {
      catalog.value.dialogue_tree_ids = [
        ...catalog.value.dialogue_tree_ids,
        ...page.dialogue_tree_ids,
      ];
    } else {
      catalog.value.style_rubrics = [...catalog.value.style_rubrics, ...page.style_rubrics];
    }
    catalogTotals.value = {
      ...catalogTotals.value,
      [kind]: page.total ?? catalogLoaded(kind),
    };
    catalogNextCursors.value = {
      ...catalogNextCursors.value,
      [kind]: page.next_cursor ?? null,
    };
  } catch (error) {
    if (sequence !== catalogKindSequences[kind]) return;
    if (error instanceof ApiError && [409, 422].includes(error.status)) {
      await loadCatalog();
      return;
    }
    catalogError.value = errorMessage(error);
  } finally {
    if (sequence === catalogKindSequences[kind]) {
      catalogLoadingMore.value = { ...catalogLoadingMore.value, [kind]: false };
    }
  }
}

async function runQuestExplore(): Promise<void> {
  if (!questId.value || questBusy.value) return;
  questBusy.value = true;
  questError.value = "";
  questResult.value = null;
  try {
    questResult.value = await apiPost<Record<string, unknown>>(
      `${prefix.value}/quests/${encodeURIComponent(questId.value)}:explore`,
      { max_states: questMaxStates.value, max_depth: questMaxDepth.value },
    );
  } catch (error) {
    questError.value = errorMessage(error);
  } finally {
    questBusy.value = false;
  }
}

async function runSpecTranslate(): Promise<void> {
  const selectedQuest = questId.value;
  const intent = specIntent.value.trim();
  if (!selectedQuest || !intent || specBusy.value) return;
  specBusy.value = true;
  specError.value = "";
  specSubmitError.value = "";
  specResult.value = null;
  specSubmitResult.value = null;
  specTranslatedQuestId.value = "";
  try {
    const body = await apiPost<SpecTranslationResult>(
      `${prefix.value}/quests/${encodeURIComponent(selectedQuest)}/specs:translate`,
      {
        intent,
        use_llm: specUseModel.value,
        ...(specUseModel.value ? llmParams() : { llm_mode: "offline" }),
        ...(specUseModel.value && specMaxCostUsd.value !== ""
          ? { max_cost_usd: Number(specMaxCostUsd.value) }
          : {}),
      },
    );
    specResult.value = body;
    specTranslatedQuestId.value = selectedQuest;
    addSessionCost(costOf(body));
  } catch (error) {
    specError.value = errorMessage(error);
  } finally {
    specBusy.value = false;
  }
}

async function submitTemporalSpec(): Promise<void> {
  const translated = specResult.value;
  const selectedQuest = specTranslatedQuestId.value;
  if (
    specSubmitting.value
    || !selectedQuest
    || !translated
    || translated.outcome.status !== "needs_confirmation"
    || !translated.outcome.spec
  ) return;
  specSubmitting.value = true;
  specSubmitError.value = "";
  specSubmitResult.value = null;
  try {
    specSubmitResult.value = await apiPost<SpecSubmitResult>(
      `${prefix.value}/quests/${encodeURIComponent(selectedQuest)}/specs:submit`,
      {
        intent: translated.outcome.intent,
        draft: translated.outcome.draft,
      },
    );
    pushToast("时序规约草稿已送入审阅队列，正典尚未修改。", "ok");
  } catch (error) {
    specSubmitError.value = errorMessage(error);
  } finally {
    specSubmitting.value = false;
  }
}

async function runDialogueWalk(): Promise<void> {
  if (!dialogueId.value || dialogueBusy.value) return;
  dialogueBusy.value = true;
  dialogueError.value = "";
  dialogueResult.value = null;
  try {
    const initialState = asObject(dialogueInitialState.value);
    const choices = dialogueChoices.value
      .split(/[\n,]+/)
      .map((choice) => choice.trim())
      .filter(Boolean);
    dialogueResult.value = await apiPost<Record<string, unknown>>(
      `${prefix.value}/dialogues/${encodeURIComponent(dialogueId.value)}:walk`,
      {
        mode: dialogueMode.value,
        choices: dialogueMode.value === "walk" ? choices : [],
        initial_state: initialState,
        max_endings: dialogueMaxEndings.value,
      },
    );
  } catch (error) {
    dialogueError.value = errorMessage(error);
  } finally {
    dialogueBusy.value = false;
  }
}

async function runFlagXref(): Promise<void> {
  if (flagBusy.value) return;
  flagBusy.value = true;
  flagError.value = "";
  flagResult.value = null;
  const query = new URLSearchParams();
  if (flagName.value.trim()) query.set("flag", flagName.value.trim());
  try {
    flagResult.value = await apiGet<Record<string, unknown>>(
      `${prefix.value}/flags:xref${query.size ? `?${query.toString()}` : ""}`,
    );
  } catch (error) {
    flagError.value = errorMessage(error);
  } finally {
    flagBusy.value = false;
  }
}

async function runRepairPlan(): Promise<void> {
  if (repairBusy.value) return;
  repairBusy.value = true;
  repairError.value = "";
  repairResult.value = null;
  try {
    const body = await apiPost<RepairPlanResult>(
      `${prefix.value}/repairs:plan`,
      {
        max_iterations: repairIterations.value,
        max_depth: repairDepth.value === "" ? null : repairDepth.value,
        seed: repairSeed.value,
        llm_candidates: repairUseModel.value,
        max_llm_calls: repairMaxModelCalls.value,
        ...(repairUseModel.value ? llmParams() : { llm_mode: "offline" }),
        ...(repairUseModel.value && repairMaxCostUsd.value !== ""
          ? { max_cost_usd: Number(repairMaxCostUsd.value) }
          : {}),
      },
    );
    repairResult.value = body;
    addSessionCost(costOf(body));
  } catch (error) {
    repairError.value = errorMessage(error);
  } finally {
    repairBusy.value = false;
  }
}

async function runStyleSweep(): Promise<void> {
  if (!rubricId.value || styleBusy.value) return;
  styleBusy.value = true;
  styleError.value = "";
  styleResult.value = null;
  try {
    styleResult.value = await apiPost<StyleSweepResult>(
      `${prefix.value}/styles/${encodeURIComponent(rubricId.value)}:sweep`,
      {
        semantic_threshold: semanticThreshold.value,
        use_llm: styleUseJudge.value,
        max_judge: styleMaxJudge.value,
        ...(styleUseJudge.value ? llmParams() : { llm_mode: "offline" }),
        ...(styleUseJudge.value && styleMaxCostUsd.value !== ""
          ? { max_cost_usd: Number(styleMaxCostUsd.value) }
          : {}),
      },
    );
    addSessionCost(costOf(styleResult.value));
  } catch (error) {
    styleError.value = errorMessage(error);
  } finally {
    styleBusy.value = false;
  }
}

async function runQualityHarness(): Promise<void> {
  if (qualityBusy.value) return;
  qualityBusy.value = true;
  qualityError.value = "";
  qualityResult.value = null;
  try {
    qualityResult.value = await apiPost<QualityResult>(`${prefix.value}/quality:run`, {
      propose_fixes: proposeFixes.value,
      max_issues: qualityMaxIssues.value,
      max_candidates_per_issue: qualityCandidates.value,
    });
  } catch (error) {
    qualityError.value = errorMessage(error);
  } finally {
    qualityBusy.value = false;
  }
}

async function loadAgentRuns(
  cursor: string | null = null,
  requestedPage = 1,
  retryStale = true,
): Promise<void> {
  const sequence = ++historyLoadSequence;
  historyLoading.value = true;
  historyError.value = "";
  const query = new URLSearchParams({ limit: String(HISTORY_PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  try {
    const body = await apiGet<AgentRunsResponse>(
      `${prefix.value}/agent-runs?${query.toString()}`,
    );
    if (sequence !== historyLoadSequence) return;
    runs.value = body.runs;
    runTotal.value = body.total;
    nextRunCursor.value = body.next_cursor;
    previousRunCursor.value = body.previous_cursor;
    currentRunCursor.value = body.page_cursor ?? cursor ?? "";
    runSnapshot.value = body.snapshot_etag;
    runPage.value = requestedPage;
    selectedRun.value = null;
  } catch (error) {
    if (sequence !== historyLoadSequence) return;
    if (error instanceof ApiError && error.status === 409 && cursor && retryStale) {
      runCursorTrail.value = [""];
      pushToast("运行历史在翻页期间发生变化，已回到最新第一页。", "info");
      await loadAgentRuns(null, 1, false);
      return;
    }
    historyError.value = errorMessage(error);
  } finally {
    if (sequence === historyLoadSequence) historyLoading.value = false;
  }
}

async function nextAgentRuns(): Promise<void> {
  const cursor = nextRunCursor.value;
  if (!cursor || historyLoading.value) return;
  runCursorTrail.value[runPage.value] = cursor;
  await loadAgentRuns(cursor, runPage.value + 1);
}

async function previousAgentRuns(): Promise<void> {
  if (runPage.value <= 1 || historyLoading.value) return;
  const known = runCursorTrail.value[runPage.value - 2] || null;
  await loadAgentRuns(previousRunCursor.value ?? known, runPage.value - 1);
}

async function openAgentRun(runId: string): Promise<void> {
  if (detailLoading.value) return;
  detailLoading.value = true;
  try {
    const body = await apiGet<{ run: AgentRunDetail }>(
      `${prefix.value}/agent-runs/${encodeURIComponent(runId)}`,
    );
    selectedRun.value = body.run;
  } catch (error) {
    historyError.value = errorMessage(error);
  } finally {
    detailLoading.value = false;
  }
}

function formatTime(value: string): string {
  const parsed = new Date(value);
  return Number.isNaN(parsed.valueOf()) ? value : parsed.toLocaleString();
}

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  await Promise.all([loadCatalog(), loadAgentRuns()]);
});

onUnmounted(() => {
  historyLoadSequence += 1;
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});
</script>

<template>
  <section>
    <PageHead
      overline="ENGINEERING LAB"
      title="工程实验室"
      purpose="逐步运行任务逻辑、规约编写、对白、旗标、MCTS 修复搜索、风格清单和质量闭环；中间证据可复核，写入始终经过人工审阅。"
    />

    <div class="safety pane" role="note">
      <b>安全边界</b>
      <span>任务探索、规约翻译、对白游走、旗标与风格扫描只读；规约与修复只能先送审，任何工具都不会在这里直接修改正典。</span>
    </div>

    <div v-if="catalogError" class="error-banner pane" role="alert">
      <span>{{ catalogError }}</span>
      <button type="button" @click="loadCatalog">重试</button>
    </div>

    <div class="tool-tabs" role="tablist" aria-label="工程工具">
      <button
        v-for="tool in TOOLS"
        :key="tool.key"
        type="button"
        role="tab"
        :aria-selected="active === tool.key"
        :class="{ active: active === tool.key }"
        @click="active = tool.key"
      >
        <b>{{ tool.label }}</b>
        <span>{{ tool.hint }}</span>
      </button>
    </div>

    <div v-if="catalogLoading" class="pane loading" role="status">
      <span class="ow-spinner" aria-hidden="true"></span>
      <span>正在读取工程对象目录…</span>
    </div>

    <div v-else-if="active === 'quest'" class="workspace">
      <form class="pane controls" @submit.prevent="runQuestExplore">
        <div class="section"><span class="t">任务状态穷举</span></div>
        <p class="muted">复用正式逻辑求值器，在上限内搜索所有可达分支、结局与见证路径。</p>
        <label>
          <span>任务</span>
          <select v-model="questId" :disabled="!catalog?.quest_ids.length">
            <option v-if="!catalog?.quest_ids.length" value="">当前世界没有任务</option>
            <option v-for="id in catalog?.quest_ids" :key="id" :value="id">{{ id }}</option>
          </select>
        </label>
        <div class="catalog-page-meta">
          <span>已加载 {{ catalogLoaded("quest") }} / {{ catalogTotals.quest }}</span>
          <button
            v-if="catalogNextCursors.quest"
            type="button"
            :disabled="catalogLoadingMore.quest"
            @click="loadMoreCatalog('quest')"
          >{{ catalogLoadingMore.quest ? "读取中…" : "加载更多任务" }}</button>
        </div>
        <div class="field-grid">
          <label><span>最大状态数</span><input v-model.number="questMaxStates" type="number" min="1" max="1000000" /></label>
          <label><span>最大深度</span><input v-model.number="questMaxDepth" type="number" min="1" max="100000" /></label>
        </div>
        <button class="primary" :disabled="questBusy || !questId" type="submit">
          {{ questBusy ? "搜索中…" : "穷举可达路径" }}
        </button>
        <p v-if="questError" class="error" role="alert">{{ questError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">探索报告</span></div>
        <pre v-if="questResult">{{ pretty(questResult.report) }}</pre>
        <p v-else class="empty">运行后显示可达结局、见证路径与截断原因。</p>
      </div>
    </div>

    <div v-else-if="active === 'spec'" class="workspace">
      <form class="pane controls" @submit.prevent="runSpecTranslate">
        <div class="section"><span class="t">策划意图 → 时序规约</span></div>
        <p class="muted">
          模型只负责把大白话分类成受限草稿；旗标接地、公式组装、类型检查和回译均由确定性代码执行。
        </p>
        <label>
          <span>任务</span>
          <select v-model="questId" :disabled="!catalog?.quest_ids.length">
            <option v-if="!catalog?.quest_ids.length" value="">当前世界没有任务</option>
            <option v-for="id in catalog?.quest_ids" :key="id" :value="id">{{ id }}</option>
          </select>
        </label>
        <div class="catalog-page-meta">
          <span>已加载 {{ catalogLoaded("quest") }} / {{ catalogTotals.quest }}</span>
          <button
            v-if="catalogNextCursors.quest"
            type="button"
            :disabled="catalogLoadingMore.quest"
            @click="loadMoreCatalog('quest')"
          >{{ catalogLoadingMore.quest ? "读取中…" : "加载更多任务" }}</button>
        </div>
        <label>
          <span>策划意图</span>
          <textarea
            v-model="specIntent"
            rows="5"
            maxlength="1200"
            required
            placeholder="例如：『shared』应当可达"
          ></textarea>
        </label>
        <label class="check model-control">
          <input v-model="specUseModel" type="checkbox" :disabled="!llmReady" />
          <span>用真实模型识别模式与意图中的状态名称</span>
        </label>
        <p v-if="!llmReady" class="model-note">
          真实翻译不可用：请先在 <RouterLink to="/settings">设置</RouterLink> 中完成连接；带引号的确定性翻译仍可运行。
        </p>
        <label v-if="specUseModel">
          <span>成本结算阈值（USD，可空）</span>
          <input v-model.number="specMaxCostUsd" type="number" min="0" step="0.001" placeholder="不限" />
        </label>
        <p class="boundary">
          这里不会写正典。先检查结构草稿、封闭世界接地、组装公式与回译；只有确认回译后才能送审，最终仍需审阅台人工接受。
        </p>
        <button class="primary" :disabled="specBusy || !questId || !specIntent.trim()" type="submit">
          {{ specBusy ? "逐步翻译中…" : "运行翻译与校验" }}
        </button>
        <p v-if="specError" class="error" role="alert">{{ specError }}</p>
      </form>
      <div class="pane output spec-output">
        <div class="section"><span class="t">可复核流水线</span></div>
        <template v-if="specResult">
          <div class="status-row">
            <span class="chip">任务：{{ specTranslatedQuestId }}</span>
            <span class="chip">翻译器：{{ specResult.translator_mode }}</span>
            <span class="chip">LLM：{{ specResult.used_llm ? `实际调用 ${specResult.telemetry.calls} 次` : "未调用" }}</span>
            <span class="chip safe">正典未写入</span>
            <span v-if="specResult.degraded" class="chip warning">
              模型输出无效，已显式降级：{{ specResult.degradation_reasons.join("、") }}
            </span>
            <span class="chip">成本 ${{ specResult.cost_budget.used_usd.toFixed(6) }}{{ specResult.cost_budget.cost_is_estimate ? "（估算）" : "" }}</span>
          </div>
          <ol class="spec-steps">
            <li>
              <b>1 · 受限结构草稿</b>
              <span>模式 {{ specResult.outcome.draft.pattern }}；操作数 {{ specResult.outcome.draft.operands.join("、") || "无" }}</span>
              <small>{{ specResult.outcome.draft.description }}</small>
            </li>
            <li>
              <b>2 · 封闭世界旗标接地</b>
              <div v-if="specResult.outcome.groundings.length" class="grounding-list">
                <span v-for="grounding in specResult.outcome.groundings" :key="`${grounding.surface}:${grounding.flag_id}`">
                  <code>{{ grounding.surface }}</code>
                  → <code>{{ grounding.flag_id || "未接地" }}</code>
                  · {{ grounding.status }} · {{ grounding.confidence.toFixed(2) }}
                  <small v-if="grounding.note">{{ grounding.note }}</small>
                </span>
              </div>
              <span v-else>此模式不需要操作数，或没有识别到可接地符号。</span>
            </li>
            <li>
              <b>3 · 确定性组装与类型检查</b>
              <pre v-if="specResult.outcome.spec">{{ pretty(specResult.outcome.spec) }}</pre>
              <span v-else>{{ specResult.outcome.message }}</span>
            </li>
            <li>
              <b>4 · 回译确认</b>
              <blockquote v-if="specResult.outcome.back_translation">{{ specResult.outcome.back_translation }}</blockquote>
              <span v-else>{{ specResult.outcome.message }}</span>
            </li>
            <li>
              <b>5 · 人工送审</b>
              <p>{{ specResult.outcome.message }}</p>
              <button
                v-if="specResult.outcome.status === 'needs_confirmation' && specResult.outcome.spec"
                class="primary"
                type="button"
                :disabled="specSubmitting || !!specSubmitResult"
                @click="submitTemporalSpec"
              >{{ specSubmitting ? "服务器重新校验并送审中…" : specSubmitResult ? "已送审" : "确认回译并送审" }}</button>
              <span v-else class="chip warning">{{ specResult.outcome.status }}</span>
              <p v-if="specSubmitError" class="error" role="alert">{{ specSubmitError }}</p>
              <div v-if="specSubmitResult" class="submitted" role="status">
                <span>审阅项 <code>{{ specSubmitResult.review_item_id }}</code> 已创建，正典仍未写入。</span>
                <RouterLink class="review-link" to="/review">前往「审阅台」人工接受</RouterLink>
              </div>
            </li>
          </ol>
        </template>
        <p v-else class="empty">输入意图后，会依次显示模型边界、接地证据、可解析公式、回译和人工送审门。</p>
      </div>
    </div>

    <div v-else-if="active === 'dialogue'" class="workspace">
      <form class="pane controls" @submit.prevent="runDialogueWalk">
        <div class="section"><span class="t">对白路径执行</span></div>
        <label>
          <span>对白树</span>
          <select v-model="dialogueId" :disabled="!catalog?.dialogue_tree_ids.length">
            <option v-if="!catalog?.dialogue_tree_ids.length" value="">当前世界没有对白树</option>
            <option v-for="id in catalog?.dialogue_tree_ids" :key="id" :value="id">{{ id }}</option>
          </select>
        </label>
        <div class="catalog-page-meta">
          <span>已加载 {{ catalogLoaded("dialogue_tree") }} / {{ catalogTotals.dialogue_tree }}</span>
          <button
            v-if="catalogNextCursors.dialogue_tree"
            type="button"
            :disabled="catalogLoadingMore.dialogue_tree"
            @click="loadMoreCatalog('dialogue_tree')"
          >{{ catalogLoadingMore.dialogue_tree ? "读取中…" : "加载更多对白树" }}</button>
        </div>
        <label>
          <span>模式</span>
          <select v-model="dialogueMode">
            <option value="walk">固定选择脚本</option>
            <option value="explore">枚举所有可满足结局</option>
          </select>
        </label>
        <label v-if="dialogueMode === 'walk'">
          <span>选择脚本（每行一个 node:index）</span>
          <textarea v-model="dialogueChoices" rows="4" placeholder="n0:0&#10;n1:1"></textarea>
        </label>
        <label>
          <span>初始状态（JSON）</span>
          <textarea v-model="dialogueInitialState" rows="4" spellcheck="false"></textarea>
        </label>
        <label v-if="dialogueMode === 'explore'">
          <span>最大结局数</span>
          <input v-model.number="dialogueMaxEndings" type="number" min="1" max="10000" />
        </label>
        <button class="primary" :disabled="dialogueBusy || !dialogueId" type="submit">
          {{ dialogueBusy ? "执行中…" : dialogueMode === "explore" ? "枚举结局" : "执行路径" }}
        </button>
        <p v-if="dialogueError" class="error" role="alert">{{ dialogueError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">对白执行结果</span></div>
        <pre v-if="dialogueResult">{{ pretty(dialogueResult.result) }}</pre>
        <p v-else class="empty">运行后显示节点路径、状态变化、覆盖选择和终止状态。</p>
      </div>
    </div>

    <div v-else-if="active === 'flag'" class="workspace">
      <form class="pane controls" @submit.prevent="runFlagXref">
        <div class="section"><span class="t">旗标 Where-used</span></div>
        <p class="muted">留空可生成全世界旗标清单；填写名称则聚焦一项并验证读写链路。</p>
        <label><span>旗标（可选）</span><input v-model="flagName" maxlength="200" placeholder="例如：gate_open" /></label>
        <button class="primary" :disabled="flagBusy" type="submit">{{ flagBusy ? "分析中…" : "生成引用报告" }}</button>
        <p v-if="flagError" class="error" role="alert">{{ flagError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">声明 · 读取 · 写入</span></div>
        <pre v-if="flagResult">{{ pretty(flagResult) }}</pre>
        <p v-else class="empty">运行后显示变量声明位置、所有读者和写者，以及无法解析的表达式。</p>
      </div>
    </div>

    <div v-else-if="active === 'repair'" class="workspace">
      <form class="pane controls" @submit.prevent="runRepairPlan">
        <div class="section"><span class="t">MCTS 修复规划</span></div>
        <p class="muted">默认由确定性修复器提供候选；可显式接入模型扩宽候选空间。所有候选仍经过影子验证，且此处只返回计划。</p>
        <div class="field-grid">
          <label><span>迭代次数</span><input v-model.number="repairIterations" type="number" min="0" max="2000" /></label>
          <label><span>最大深度（可空）</span><input v-model.number="repairDepth" type="number" min="1" max="100" placeholder="自动" /></label>
          <label><span>随机种子</span><input v-model.number="repairSeed" type="number" /></label>
        </div>
        <label class="check model-control">
          <input v-model="repairUseModel" type="checkbox" :disabled="!llmReady" />
          <span>用真实模型生成额外修复候选</span>
        </label>
        <p v-if="!llmReady" class="model-note">
          模型候选不可用：请先在 <RouterLink to="/settings">设置</RouterLink> 中完成连接。确定性规划仍可运行。
        </p>
        <div v-if="repairUseModel" class="field-grid">
          <label><span>模型调用硬上限</span><input v-model.number="repairMaxModelCalls" type="number" min="1" max="128" /></label>
          <label><span>成本结算阈值（USD，可空）</span><input v-model.number="repairMaxCostUsd" type="number" min="0" step="0.001" placeholder="不限" /></label>
        </div>
        <p v-if="repairUseModel" class="boundary">模型最多派发 {{ repairMaxModelCalls }} 次；成本值是运行后结算阈值，超出会明确标记，不伪装成预扣硬停。</p>
        <button class="primary" :disabled="repairBusy" type="submit">{{ repairBusy ? "搜索中…" : "生成修复计划" }}</button>
        <p v-if="repairError" class="error" role="alert">{{ repairError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">只读计划</span></div>
        <div v-if="repairResult" class="status-row">
          <span class="chip">候选：{{ repairResult.candidate_source }}</span>
          <span class="chip safe">未写正典</span>
          <span class="chip">LLM：{{ repairResult.used_llm ? `实际调用 ${repairResult.telemetry.calls} 次` : "未调用" }}</span>
          <span class="chip">成本 ${{ repairResult.cost_budget.used_usd.toFixed(6) }}{{ repairResult.cost_budget.cost_is_estimate ? "（估算）" : "" }}</span>
          <span v-if="repairResult.cost_budget.over_budget" class="chip warning">超过结算阈值</span>
        </div>
        <pre v-if="repairResult">{{ pretty(repairResult.plan) }}</pre>
        <p v-else class="empty">运行后显示初始/最终错误数、搜索轨迹与拟议修复步骤。</p>
      </div>
    </div>

    <div v-else-if="active === 'style'" class="workspace">
      <form class="pane controls" @submit.prevent="runStyleSweep">
        <div class="section"><span class="t">StyleRubric 风格清查</span></div>
        <label>
          <span>版本化风格清单</span>
          <select v-model="rubricId" :disabled="!catalog?.style_rubrics.length">
            <option v-if="!catalog?.style_rubrics.length" value="">当前世界没有 StyleRubric</option>
            <option v-for="rubric in catalog?.style_rubrics" :key="rubric.id" :value="rubric.id">
              {{ rubric.label }} · v{{ rubric.version }} · {{ rubric.element_count }} 项
            </option>
          </select>
        </label>
        <div class="catalog-page-meta">
          <span>已加载 {{ catalogLoaded("style_rubric") }} / {{ catalogTotals.style_rubric }}</span>
          <button
            v-if="catalogNextCursors.style_rubric"
            type="button"
            :disabled="catalogLoadingMore.style_rubric"
            @click="loadMoreCatalog('style_rubric')"
          >{{ catalogLoadingMore.style_rubric ? "读取中…" : "加载更多清单" }}</button>
        </div>
        <label><span>语义阈值 {{ semanticThreshold.toFixed(2) }}</span><input v-model.number="semanticThreshold" type="range" min="0" max="1" step="0.05" /></label>
        <label class="check model-control">
          <input v-model="styleUseJudge" type="checkbox" :disabled="!llmReady" />
          <span>用真实模型执行 L3 逐项判定</span>
        </label>
        <p v-if="!llmReady" class="model-note">
          L3 判定不可用：请先在 <RouterLink to="/settings">设置</RouterLink> 中完成连接；L1/L2 本地级联不受影响。
        </p>
        <div v-if="styleUseJudge" class="field-grid">
          <label><span>最多判定候选</span><input v-model.number="styleMaxJudge" type="number" min="1" max="2000" /></label>
          <label><span>成本结算阈值（USD，可空）</span><input v-model.number="styleMaxCostUsd" type="number" min="0" step="0.001" placeholder="不限" /></label>
        </div>
        <p class="muted">始终运行本地 L1/L2；只有显式开启且模型已连接时才运行 L3，结果会报告实际调用数与漏判上限。</p>
        <button class="primary" :disabled="styleBusy || !rubricId" type="submit">{{ styleBusy ? "扫描中…" : "执行风格清查" }}</button>
        <p v-if="styleError" class="error" role="alert">{{ styleError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">清查结果</span></div>
        <template v-if="styleResult">
          <div class="status-row">
            <span class="chip">扫描 {{ styleResult.scanned_total }}</span>
            <span class="chip">L1 {{ styleResult.l1_total }}</span>
            <span class="chip">L2 {{ styleResult.l2_total }}</span>
            <span class="chip">L3 {{ styleResult.judge_mode }} · 判定 {{ styleResult.judged_count }}/上限 {{ styleResult.judge_limit }}</span>
            <span class="chip">LLM：{{ styleResult.llm_used ? `实际调用 ${styleResult.telemetry.calls} 次` : "未调用" }}</span>
            <span v-if="styleResult.judge_skipped" class="chip warning">{{ styleResult.judge_skipped }} 项因上限未判</span>
            <span class="chip">已忽略 {{ styleResult.dismissed_count }}</span>
            <span class="chip">成本 ${{ styleResult.cost_budget.used_usd.toFixed(6) }}{{ styleResult.cost_budget.cost_is_estimate ? "（估算）" : "" }}</span>
            <span v-if="styleResult.cost_budget.over_budget" class="chip warning">超过结算阈值</span>
          </div>
          <pre>{{ styleResult.markdown }}</pre>
        </template>
        <p v-else class="empty">运行后显示命中对象、证据字段、规则层级和建议人工复核项。</p>
      </div>
    </div>

    <div v-else-if="active === 'quality'" class="workspace">
      <form class="pane controls" @submit.prevent="runQualityHarness">
        <div class="section"><span class="t">质量闭环</span></div>
        <label class="check"><input v-model="proposeFixes" type="checkbox" /><span>为可修复问题生成提案</span></label>
        <div class="field-grid">
          <label><span>最多处理问题</span><input v-model.number="qualityMaxIssues" type="number" min="0" max="50" /></label>
          <label><span>每问题最多候选</span><input v-model.number="qualityCandidates" type="number" min="0" max="5" /></label>
        </div>
        <p class="boundary">即使启用提案，也只会写入提案库。正典内容必须在「校勘修复」中由人工批准。</p>
        <button class="primary" :disabled="qualityBusy" type="submit">{{ qualityBusy ? "运行中…" : "运行质量闭环" }}</button>
        <p v-if="qualityError" class="error" role="alert">{{ qualityError }}</p>
      </form>
      <div class="pane output">
        <div class="section"><span class="t">质量报告</span></div>
        <template v-if="qualityResult">
          <div class="status-row">
            <span class="chip">阶段 {{ qualityResult.phase }}</span>
            <span class="chip">{{ qualityResult.stop_reason }}</span>
            <span class="chip safe">正典未写入</span>
          </div>
          <h3>修复提案（{{ qualityResult.patch_proposals.length }}）</h3>
          <div v-if="qualityResult.patch_proposals.length" class="proposal-list">
            <div v-for="(proposal, index) in qualityResult.patch_proposals" :key="proposal.patch_id ?? proposal.id ?? index" class="proposal">
              <b class="mono">{{ proposal.patch_id ?? proposal.id ?? `提案 ${index + 1}` }}</b>
              <span>{{ proposal.rationale ?? `${proposal.ops?.length ?? 0} 个操作` }}</span>
            </div>
            <RouterLink class="review-link" to="/audit">前往「校勘修复」人工审阅</RouterLink>
          </div>
          <p v-else class="empty">本次没有生成修复提案。</p>
          <details><summary>查看完整质量报告</summary><pre>{{ pretty(qualityResult) }}</pre></details>
        </template>
        <p v-else class="empty">运行后显示审计摘要、交付阻塞项、工具轨迹和待审提案。</p>
      </div>
    </div>

    <div v-else class="history-layout">
      <div class="pane history-list">
        <div class="section"><span class="t">Agent 运行历史</span></div>
        <div class="history-meta">
          <span>共 {{ runTotal }} 次</span>
          <span>第 {{ runPage }} / {{ Math.max(1, Math.ceil(runTotal / HISTORY_PAGE_SIZE)) }} 页</span>
          <button type="button" :disabled="historyLoading" @click="loadAgentRuns(null, 1)">刷新</button>
        </div>
        <p v-if="historyError" class="error" role="alert">{{ historyError }}</p>
        <div v-if="historyLoading" class="loading compact" role="status"><span class="ow-spinner"></span>读取运行记录…</div>
        <p v-else-if="!runs.length" class="empty">还没有持久化的 Agent 运行。</p>
        <button
          v-for="run in runs"
          v-else
          :key="run.id"
          type="button"
          class="run-row"
          :class="{ selected: selectedRun?.id === run.id }"
          @click="openAgentRun(run.id)"
        >
          <span class="run-main"><b>{{ run.goal }}</b><small class="mono">{{ run.id }}</small></span>
          <span class="run-stats"><span>{{ run.agent_name }}</span><span>{{ run.step_count }} 步</span><span>${{ run.cost_usd.toFixed(4) }}</span></span>
          <time>{{ formatTime(run.created_at) }}</time>
        </button>
        <nav class="pager" aria-label="Agent 运行历史分页">
          <button type="button" :disabled="historyLoading || runPage <= 1" @click="previousAgentRuns">上一页</button>
          <span class="mono">{{ runPage }}</span>
          <button type="button" :disabled="historyLoading || !nextRunCursor" @click="nextAgentRuns">下一页</button>
        </nav>
        <span class="snapshot mono" :title="runSnapshot">快照 {{ runSnapshot.slice(0, 12) || "—" }}</span>
      </div>
      <div class="pane run-detail">
        <div class="section"><span class="t">运行详情</span></div>
        <div v-if="detailLoading" class="loading compact" role="status"><span class="ow-spinner"></span>读取完整轨迹…</div>
        <template v-else-if="selectedRun">
          <h2>{{ selectedRun.goal }}</h2>
          <div class="status-row"><span class="chip">{{ selectedRun.stop_reason }}</span><span class="chip">{{ selectedRun.step_count }} 步</span><span class="chip">{{ selectedRun.latency_ms.toFixed(0) }} ms</span></div>
          <pre>{{ pretty(selectedRun.transcript) }}</pre>
        </template>
        <p v-else class="empty">选择左侧运行记录，查看完整 transcript 和工具步骤。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.safety { margin-bottom: 1rem; padding: 0.85rem 1.1rem; display: flex; gap: 0.75rem; color: var(--ow-ink-soft); font-size: 0.84rem; line-height: 1.55; }
.safety b { flex: none; color: var(--ow-gold-bright); }
.error-banner { display: flex; align-items: center; justify-content: space-between; gap: 1rem; margin-bottom: 1rem; padding: 0.8rem 1rem; color: #efae8e; }
.error-banner button, .history-meta button { border: 1px solid var(--ow-line); color: var(--ow-ink); background: transparent; padding: 0.35rem 0.6rem; cursor: pointer; }
.tool-tabs { display: grid; grid-template-columns: repeat(8, minmax(0, 1fr)); gap: 0.4rem; margin-bottom: 1rem; }
.tool-tabs button { min-height: 72px; padding: 0.65rem; border: 1px solid var(--ow-line); border-radius: 2px; color: var(--ow-ink-soft); background: rgba(10, 14, 36, 0.55); text-align: left; cursor: pointer; }
.tool-tabs button:hover { border-color: var(--ow-gold-soft); }
.tool-tabs button.active { border-color: var(--ow-gold); color: var(--ow-gold-bright); background: var(--ow-gold-faint); box-shadow: inset 0 -2px 0 var(--ow-gold); }
.tool-tabs b, .tool-tabs span { display: block; }
.tool-tabs b { font-size: 0.86rem; }
.tool-tabs span { margin-top: 0.25rem; color: var(--ow-muted); font-size: 0.68rem; line-height: 1.35; }
.workspace, .history-layout { display: grid; grid-template-columns: minmax(280px, 0.78fr) minmax(0, 1.35fr); gap: 1rem; align-items: start; }
.controls, .output, .history-list, .run-detail { min-width: 0; padding: 1rem 1.15rem 1.15rem; }
.controls { display: flex; flex-direction: column; gap: 0.8rem; }
.catalog-page-meta {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.65rem;
  margin-top: -0.35rem;
  color: var(--ow-muted);
  font-size: 0.72rem;
}
.catalog-page-meta button {
  min-height: 32px;
  padding: 0.3rem 0.55rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-cyan);
  background: transparent;
  cursor: pointer;
}
.catalog-page-meta button:disabled { opacity: 0.55; cursor: wait; }
.controls > .section, .output > .section, .history-list > .section, .run-detail > .section { width: 100%; margin-top: 0; }
label { display: flex; flex-direction: column; gap: 0.35rem; color: var(--ow-ink-soft); font-size: 0.78rem; }
input, select, textarea { width: 100%; border: 1px solid var(--ow-line); border-radius: 2px; color: var(--ow-ink); background: var(--ow-panel-2); padding: 0.58rem 0.65rem; font: inherit; }
textarea { resize: vertical; font-family: var(--ow-num); font-size: 0.8rem; }
input[type="range"] { padding: 0; accent-color: var(--ow-gold-bright); }
.field-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 0.65rem; }
.check { flex-direction: row; align-items: center; }
.check input { width: auto; }
.model-control { padding: 0.65rem 0.7rem; border: 1px solid var(--ow-line); background: rgba(143, 214, 232, 0.04); }
.model-note { margin: -0.35rem 0 0; color: var(--ow-muted); font-size: 0.75rem; line-height: 1.5; }
.model-note a { color: var(--ow-cyan); }
.primary { align-self: flex-start; min-height: 40px; padding: 0.55rem 0.9rem; border: 1px solid var(--ow-gold); color: var(--ow-night-deep); background: var(--ow-gold-bright); font-weight: 700; cursor: pointer; }
.primary:disabled { opacity: 0.5; cursor: not-allowed; }
.muted, .boundary { margin: 0; font-size: 0.78rem; line-height: 1.55; }
.boundary { padding: 0.6rem 0.7rem; border-left: 2px solid var(--ow-gold); color: #d8c99f; background: var(--ow-gold-faint); }
.error { margin: 0; color: #efae8e; font-size: 0.8rem; line-height: 1.5; }
.output { min-height: 360px; }
.output pre, .run-detail pre { max-height: 64vh; margin: 0.75rem 0 0; padding: 0.8rem; overflow: auto; border: 1px solid var(--ow-line); color: #cbd7e8; background: rgba(4, 7, 22, 0.56); font: 0.75rem/1.55 var(--ow-num); white-space: pre-wrap; overflow-wrap: anywhere; }
.empty { margin: 1.4rem 0; color: var(--ow-muted); font-size: 0.82rem; line-height: 1.6; }
.loading { display: flex; align-items: center; justify-content: center; gap: 0.7rem; min-height: 180px; color: var(--ow-muted); }
.loading.compact { min-height: 80px; }
.loading .ow-spinner { width: 20px; height: 20px; }
.status-row { display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 0.6rem 0; }
.chip { padding: 0.25rem 0.5rem; border: 1px solid var(--ow-line); color: var(--ow-ink-soft); background: rgba(143, 214, 232, 0.04); font-size: 0.72rem; }
.chip.safe { border-color: rgba(142, 212, 172, 0.45); color: #8ed4ac; }
.chip.warning { border-color: rgba(239, 174, 142, 0.5); color: #efae8e; }
.output h3 { margin: 1rem 0 0.5rem; font-size: 0.9rem; }
.proposal-list { display: flex; flex-direction: column; gap: 0.45rem; }
.proposal { display: flex; flex-direction: column; gap: 0.2rem; padding: 0.6rem 0.7rem; border-left: 2px solid var(--ow-cyan); background: rgba(143, 214, 232, 0.05); font-size: 0.77rem; }
.review-link { align-self: flex-start; margin-top: 0.35rem; padding: 0.45rem 0.65rem; border: 1px solid var(--ow-gold-soft); color: var(--ow-gold-bright); text-decoration: none; }
.spec-steps { display: flex; flex-direction: column; gap: 0.7rem; margin: 0.85rem 0 0; padding: 0; list-style: none; }
.spec-steps > li { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.75rem; border-left: 2px solid var(--ow-cyan); background: rgba(143, 214, 232, 0.04); color: var(--ow-ink-soft); font-size: 0.78rem; line-height: 1.55; }
.spec-steps b { color: var(--ow-cyan); }
.spec-steps small { color: var(--ow-muted); }
.spec-steps p { margin: 0; }
.spec-steps pre { max-height: 260px; margin-top: 0; }
.spec-steps blockquote { margin: 0; padding: 0.65rem 0.75rem; border: 1px solid var(--ow-gold-soft); color: var(--ow-gold-bright); background: var(--ow-gold-faint); }
.grounding-list { display: flex; flex-direction: column; gap: 0.35rem; }
.grounding-list > span { display: flex; flex-wrap: wrap; gap: 0.3rem; align-items: baseline; }
.grounding-list small { flex-basis: 100%; }
.submitted { display: flex; flex-direction: column; align-items: flex-start; gap: 0.45rem; padding: 0.65rem; border: 1px solid rgba(142, 212, 172, 0.45); color: #8ed4ac; }
details { margin-top: 0.9rem; }
summary { color: var(--ow-cyan); cursor: pointer; font-size: 0.78rem; }
.history-meta { display: flex; align-items: center; gap: 0.65rem; margin-bottom: 0.65rem; color: var(--ow-muted); font-size: 0.73rem; }
.history-meta button { margin-left: auto; }
.run-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 0.35rem 0.8rem; width: 100%; padding: 0.65rem 0.7rem; border: 0; border-bottom: 1px solid var(--ow-line); color: var(--ow-ink); background: transparent; text-align: left; cursor: pointer; }
.run-row:hover, .run-row.selected { background: var(--ow-gold-faint); }
.run-row.selected { box-shadow: inset 2px 0 0 var(--ow-gold-bright); }
.run-main { min-width: 0; display: flex; flex-direction: column; gap: 0.2rem; }
.run-main b { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 0.8rem; }
.run-main small, .snapshot { color: var(--ow-muted); font-size: 0.66rem; }
.run-stats { display: flex; gap: 0.4rem; color: var(--ow-cyan); font-size: 0.68rem; }
.run-row time { grid-column: 1 / -1; color: var(--ow-muted); font-size: 0.66rem; }
.pager { display: flex; align-items: center; justify-content: center; gap: 0.7rem; margin: 0.85rem 0 0.45rem; background: none; padding: 0; }
.pager button { min-height: 36px; padding: 0.4rem 0.75rem; border: 1px solid var(--ow-line); color: var(--ow-ink); background: var(--ow-panel-2); cursor: pointer; }
.pager button:disabled { opacity: 0.45; cursor: not-allowed; }
.snapshot { display: block; text-align: center; }
.run-detail h2 { margin: 0.5rem 0; font-size: 1rem; }
@media (max-width: 1100px) { .tool-tabs { grid-template-columns: repeat(4, minmax(0, 1fr)); } }
@media (max-width: 760px) { .tool-tabs { display: flex; overflow-x: auto; } .tool-tabs button { min-width: 132px; } .workspace, .history-layout { grid-template-columns: 1fr; } .safety { flex-direction: column; gap: 0.3rem; } }
</style>
