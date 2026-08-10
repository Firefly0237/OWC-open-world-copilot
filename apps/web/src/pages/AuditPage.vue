<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import {
  humanizeError,
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentOperator,
  currentProject,
  llmConfig,
  llmParams,
  setCurrentOperator,
} from "../api";
import PageHead from "../components/PageHead.vue";
import {
  buildContradictionScanPayload,
  contradictionActualModeLabel,
  contradictionScanControlError,
} from "../contradictionScanControls";
import {
  emptySemanticScanResult,
  failedSemanticScan,
  SEMANTIC_STATUS_MESSAGE,
  semanticScanStatus,
  type SemanticScanResult,
  type SemanticScanStatus,
} from "../semanticScanStatus";
import { notifyError, pushToast } from "../toast";

interface Issue {
  id: string;
  rule_code: string;
  severity: string;
  category: string;
  target_ref: string;
  message: string;
  status?: string;
  // 六要素里的定位与建议动作。后端在定位不出来时整组字段不下发，所以全是可选。
  source_path?: string;
  source_line?: number;
  node_ref?: string;
  suggested_action?: string;
}

interface Candidate {
  patch_id: string;
  issue_id?: string | null;
  source: string;
  target_resolved: boolean;
  resolved_error_count: number;
  ops: Record<string, unknown>[];
  rationale: string;
}

const SEV_LABEL: Record<string, string> = { error: "错误", warn: "警告", warning: "警告", info: "提示" };

const operator = ref(currentOperator());
const ran = ref(false);
const running = ref(false);
const totals = ref<Record<string, number>>({});
const issues = ref<Issue[]>([]);
const filter = ref("all");
const issueTotal = ref(0);
const nextIssuesCursor = ref<string | null>(null);
const loadingMoreIssues = ref(false);
let issuesLoadSequence = 0;

// per-issue suggestion state, keyed by issue id
const suggestState = ref<Record<string, { busy: boolean; candidates: Candidate[]; note: string }>>({});
// per-patch apply/rollback outcome
const patchState = ref<Record<string, { applied?: boolean; rolledBack?: boolean; note: string }>>({});
const pendingPatches = ref<Candidate[]>([]);
const nextPatchesCursor = ref<string | null>(null);
const loadingMorePatches = ref(false);
let patchesLoadSequence = 0;
const appliedPatches = ref<Candidate[]>([]);
const nextAppliedPatchesCursor = ref<string | null>(null);
const loadingMoreAppliedPatches = ref(false);
let appliedPatchesLoadSequence = 0;

const llmReady = ref(llmConfig().ready);
const contraUseRealJudge = ref(false);
const contraSemanticThreshold = ref<number | "">(0.6);
const contraMaxJudge = ref<number | "">(200);

function syncLlmReadiness(): void {
  llmReady.value = llmConfig().ready;
  if (!llmReady.value) contraUseRealJudge.value = false;
}

const filtered = computed(() => issues.value);

onMounted(() => {
  window.addEventListener("ow-llm-changed", syncLlmReadiness);
  void loadIssues();
  void loadStoredPatches();
  void loadAppliedPatches();
});

onUnmounted(() => window.removeEventListener("ow-llm-changed", syncLlmReadiness));

function storedPatchCandidate(patch: {
  id: string;
  issue_id?: string | null;
  ops: Record<string, unknown>[];
  rationale: string;
  origin?: string;
}): Candidate {
  return {
    patch_id: patch.id,
    issue_id: patch.issue_id,
    source: patch.origin?.includes("ai") ? "llm" : "deterministic",
    target_resolved: false,
    resolved_error_count: 0,
    ops: patch.ops,
    rationale: patch.rationale,
  };
}

async function loadStoredPatches(append = false): Promise<void> {
  if (append && !nextPatchesCursor.value) return;
  const sequence = ++patchesLoadSequence;
  loadingMorePatches.value = append;
  const query = new URLSearchParams({ status: "proposed", limit: "50" });
  if (append && nextPatchesCursor.value) query.set("cursor", nextPatchesCursor.value);
  try {
    const body = await apiGet<{
      patches: {
        id: string;
        issue_id?: string | null;
        ops: Record<string, unknown>[];
        rationale: string;
        origin?: string;
      }[];
      next_cursor: string | null;
    }>(`/projects/${currentProject()}/patches?${query.toString()}`);
    if (sequence !== patchesLoadSequence) return;
    const page = body.patches.map(storedPatchCandidate);
    pendingPatches.value = append ? [...pendingPatches.value, ...page] : page;
    nextPatchesCursor.value = body.next_cursor;
  } catch (error) {
    if (sequence !== patchesLoadSequence) return;
    notifyError(error);
    if (append) await loadStoredPatches(false);
  } finally {
    if (sequence === patchesLoadSequence) loadingMorePatches.value = false;
  }
}

async function loadAppliedPatches(append = false): Promise<void> {
  if (append && !nextAppliedPatchesCursor.value) return;
  const sequence = ++appliedPatchesLoadSequence;
  loadingMoreAppliedPatches.value = append;
  const query = new URLSearchParams({ status: "applied", limit: "20" });
  if (append && nextAppliedPatchesCursor.value) {
    query.set("cursor", nextAppliedPatchesCursor.value);
  }
  try {
    const body = await apiGet<{
      patches: {
        id: string;
        issue_id?: string | null;
        ops: Record<string, unknown>[];
        rationale: string;
        origin?: string;
      }[];
      next_cursor: string | null;
    }>(`/projects/${currentProject()}/patches?${query.toString()}`);
    if (sequence !== appliedPatchesLoadSequence) return;
    const page = body.patches.map(storedPatchCandidate);
    appliedPatches.value = append ? [...appliedPatches.value, ...page] : page;
    nextAppliedPatchesCursor.value = body.next_cursor;
  } catch (error) {
    if (sequence !== appliedPatchesLoadSequence) return;
    notifyError(error);
    if (append) await loadAppliedPatches(false);
  } finally {
    if (sequence === appliedPatchesLoadSequence) loadingMoreAppliedPatches.value = false;
  }
}

async function loadIssues(append = false): Promise<void> {
  if (append && !nextIssuesCursor.value) return;
  const sequence = ++issuesLoadSequence;
  loadingMoreIssues.value = append;
  const query = new URLSearchParams({ limit: "50" });
  if (filter.value !== "all") query.set("severity", filter.value);
  if (append && nextIssuesCursor.value) query.set("cursor", nextIssuesCursor.value);
  try {
    const body = await apiGet<{
      issues: Issue[];
      total: number;
      next_cursor: string | null;
    }>(`/projects/${currentProject()}/issues?${query.toString()}`);
    if (sequence !== issuesLoadSequence) return;
    issues.value = append ? [...issues.value, ...body.issues] : body.issues;
    issueTotal.value = body.total;
    nextIssuesCursor.value = body.next_cursor;
    ran.value = true;
  } catch (error) {
    if (sequence !== issuesLoadSequence) return;
    notifyError(error);
    if (append) await loadIssues(false);
  } finally {
    if (sequence === issuesLoadSequence) loadingMoreIssues.value = false;
  }
}

watch(filter, () => {
  if (ran.value) void loadIssues(false);
});

// Batch-2: semantic contradiction scan (allies-here-enemies-there, conflicting attributes)
interface Contra {
  refs: string[];
  subjects: string[];
  statements: string[];
  verdict: string;
  point: string;
  layer: string;
}
const contraScanning = ref(false);
const contraStatus = ref<SemanticScanStatus>("IDLE");
const contraRan = computed(() => contraStatus.value !== "IDLE");
const contra = ref<SemanticScanResult<Contra>>(emptySemanticScanResult<Contra>());
const CONTRA_PAGE_SIZE = 50;
const visibleContradictionCount = ref(CONTRA_PAGE_SIZE);
const visibleReviewCount = ref(CONTRA_PAGE_SIZE);
const visibleContradictions = computed(() =>
  contra.value.contradictions.slice(0, visibleContradictionCount.value),
);
const visibleContraReviews = computed(() =>
  contra.value.review_suggested.slice(0, visibleReviewCount.value),
);
const contraValidationError = computed(() =>
  contradictionScanControlError(contraSemanticThreshold.value, contraMaxJudge.value),
);
const lastContraRequest = ref<{
  useRealJudge: boolean;
  semanticThreshold: number;
  maxJudge: number;
} | null>(null);
const contraActualMode = computed(() =>
  contradictionActualModeLabel(contra.value, lastContraRequest.value?.useRealJudge ?? false),
);
async function scanContradictions(): Promise<void> {
  if (contraScanning.value) return;
  if (contraValidationError.value) {
    pushToast(contraValidationError.value, "error");
    return;
  }
  const config = llmConfig();
  llmReady.value = config.ready;
  if (!config.ready) contraUseRealJudge.value = false;
  const payload = buildContradictionScanPayload({
    semanticThreshold: contraSemanticThreshold.value,
    maxJudge: contraMaxJudge.value,
    requestRealJudge: contraUseRealJudge.value,
    llmReady: config.ready,
    llmModel: config.model,
  });
  lastContraRequest.value = {
    useRealJudge: payload.use_llm,
    semanticThreshold: payload.semantic_threshold,
    maxJudge: payload.max_judge,
  };
  visibleContradictionCount.value = CONTRA_PAGE_SIZE;
  visibleReviewCount.value = CONTRA_PAGE_SIZE;
  contraScanning.value = true;
  try {
    const body = await apiPost<typeof contra.value & { cost_budget?: Record<string, unknown> }>(
      `/projects/${currentProject()}/contradictions:scan`,
      payload,
    );
    contra.value = body;
    contraStatus.value = semanticScanStatus(body);
    if (body.cost_budget) addSessionCost(costOf(body));
  } catch (e) {
    const failed = failedSemanticScan<Contra>();
    contra.value = failed.result;
    contraStatus.value = failed.status;
    notifyError(e);
  } finally {
    contraScanning.value = false;
  }
}

async function runAudit(): Promise<void> {
  if (running.value) return;
  running.value = true;
  suggestState.value = {};
  patchState.value = {};
  try {
    const body = await apiPost<{ totals: Record<string, number> }>(
      `/projects/${currentProject()}/audits?include_details=false`,
      { persist: true },
    );
    totals.value = body.totals;
    await loadIssues();
    await loadStoredPatches();
    await loadAppliedPatches();
    ran.value = true;
  } catch (e) {
    notifyError(e);
  } finally {
    running.value = false;
  }
}

async function suggest(issue: Issue): Promise<void> {
  const cur = suggestState.value[issue.id];
  if (cur?.busy) return;
  suggestState.value = {
    ...suggestState.value,
    [issue.id]: { busy: true, candidates: cur?.candidates ?? [], note: "" },
  };
  try {
    const llm = llmReady.value
      ? { llm_mode: "real", llm_model: llmConfig().model }
      : { llm_mode: "offline" };
    const body = await apiPost<{
      candidates: Candidate[];
      rejected_count: number;
      used_llm: boolean;
      cost_budget?: { used_usd?: number };
    }>(`/projects/${currentProject()}/issues/${encodeURIComponent(issue.id)}/suggestions`, llm);
    addSessionCost(costOf(body));
    const note = body.candidates.length
      ? `${body.used_llm ? "模型+确定性" : "确定性修复器"}给出 ${body.candidates.length} 个修复候选${
          body.rejected_count ? `（另有 ${body.rejected_count} 个经测试会引入新问题，已排除）` : ""
        }`
      : `${body.rejected_count ? `给出了修复候选，但 ${body.rejected_count} 个经测试会引入新问题已排除——` : ""}暂无可用修复候选，可能需要手动处理`;
    suggestState.value = {
      ...suggestState.value,
      [issue.id]: { busy: false, candidates: body.candidates, note },
    };
  } catch (e) {
    suggestState.value = {
      ...suggestState.value,
      [issue.id]: { busy: false, candidates: cur?.candidates ?? [], note: humanizeError(e) },
    };
  }
}

async function applyPatch(candidate: Candidate): Promise<void> {
  if (!operator.value.trim()) {
    pushToast("先填上你的名字（用于记录谁批准了修复）。", "error");
    return;
  }
  setCurrentOperator(operator.value.trim());
  try {
    const body = await apiPost<{
      applied: boolean;
      reason: string;
      resolved_errors: string[];
      introduced_errors: string[];
    }>(
      `/projects/${currentProject()}/patches/${encodeURIComponent(candidate.patch_id)}:apply`,
      { operator: operator.value.trim() },
    );
    patchState.value = {
      ...patchState.value,
      [candidate.patch_id]: {
        applied: body.applied,
        note: body.applied
          ? `已应用：解决 ${body.resolved_errors.length} 个错误${body.introduced_errors.length ? `，引入 ${body.introduced_errors.length} 个` : "，未引入新错误"}`
          : `未应用：${body.reason}`,
      },
    };
    if (body.applied) {
      pendingPatches.value = pendingPatches.value.filter(
        (patch) => patch.patch_id !== candidate.patch_id,
      );
      await runAudit();
    }
  } catch (e) {
    patchState.value = { ...patchState.value, [candidate.patch_id]: { note: humanizeError(e) } };
  }
}

async function rollback(candidate: Candidate): Promise<void> {
  if (!operator.value.trim()) {
    pushToast("先填上你的名字（用于记录谁批准了回滚）。", "error");
    return;
  }
  setCurrentOperator(operator.value.trim());
  try {
    await apiPost(`/projects/${currentProject()}/patches/${encodeURIComponent(candidate.patch_id)}:rollback`, {
      operator: operator.value.trim(),
    });
    patchState.value = {
      ...patchState.value,
      [candidate.patch_id]: { rolledBack: true, note: "已回滚到应用前的状态。" },
    };
    await runAudit();
  } catch (e) {
    patchState.value = { ...patchState.value, [candidate.patch_id]: { note: humanizeError(e) } };
  }
}

function opSummary(ops: Record<string, unknown>[]): string {
  const kinds = ops.map((o) => String(o.op ?? o.action ?? o.kind ?? o.type ?? "op"));
  return kinds.join("、") || `${ops.length} 步`;
}

// per-issue tech-detail panel open state
const detailOpen = ref<Record<string, boolean>>({});
function toggleDetail(id: string): void {
  detailOpen.value = { ...detailOpen.value, [id]: !detailOpen.value[id] };
}

// Strip entity:/quest:/etc prefix for cleaner display; full value available in tooltip
function formatRef(ref: string): string {
  return ref.replace(/^[a-z_]+:/, "");
}

// 「去哪儿改」这一行：文件 + 行号 + 对话节点。后端定位不出来时留空，这里整行不显示——
// 报告只声称真定位到的那部分。
function locationOf(issue: Issue): string {
  const parts: string[] = [];
  if (issue.source_path) {
    parts.push(issue.source_line ? `${issue.source_path} 第 ${issue.source_line} 行` : issue.source_path);
  }
  if (issue.node_ref) parts.push(`对话节点 ${issue.node_ref}`);
  return parts.join("，");
}
</script>

<template>
  <section>
    <PageHead overline="AUDIT" title="校勘修复" purpose="检查一致性问题，每条给出可回滚的修复。" />

    <div class="bar">
      <input v-model="operator" class="op" placeholder="你的名字（记录谁批准了这次修复）" title="这不是密码，只是记录操作员的名字，方便日后溯源。填你的名字或昵称即可。" />
      <button class="primary" :disabled="running" @click="runAudit">
        {{ running ? "审计中…" : ran ? "重新审计" : "运行审计" }}
      </button>
      <span v-if="ran && Object.keys(totals).length" class="chips">
        <span class="chip red">错误 <b>{{ totals.error ?? 0 }}</b></span>
        <span class="chip amber">警告 <b>{{ totals.warn ?? totals.warning ?? 0 }}</b></span>
        <span class="chip">提示 <b>{{ totals.info ?? 0 }}</b></span>
      </span>
      <span v-else-if="ran" class="muted small">已读取 {{ issueTotal }} 条持久化问题；重新审计可刷新分类统计。</span>
    </div>

    <div v-if="pendingPatches.length" class="pane proposal-inbox">
      <div class="section"><span class="t">待人工处置的修复提案</span></div>
      <p class="muted small">
        这里包含校勘建议和 Multi-Agent 运行真实写入运行库的提案。逐条查看后再应用；没有任何提案会自动写入正典。
      </p>
      <div class="candidates">
        <div v-for="candidate in pendingPatches" :key="candidate.patch_id" class="candidate">
          <div class="c-head">
            <span class="src" :class="candidate.source">
              {{ candidate.source === "llm" ? "Agent 提案" : "确定性提案" }}
            </span>
            <span v-if="candidate.issue_id" class="mono">{{ candidate.issue_id }}</span>
            <span class="ops mono">{{ opSummary(candidate.ops) }}</span>
          </div>
          <p v-if="candidate.rationale" class="rationale">{{ candidate.rationale }}</p>
          <div class="c-actions">
            <button class="primary sm" @click="applyPatch(candidate)">人工批准并应用</button>
            <span v-if="patchState[candidate.patch_id]?.note" class="muted tiny">
              {{ patchState[candidate.patch_id].note }}
            </span>
          </div>
        </div>
      </div>
      <button
        v-if="nextPatchesCursor"
        class="ghost load-more"
        :disabled="loadingMorePatches"
        @click="loadStoredPatches(true)"
      >
        {{ loadingMorePatches ? "加载中…" : "加载更多修复提案" }}
      </button>
    </div>

    <div v-if="appliedPatches.length" class="pane proposal-inbox applied-inbox">
      <div class="section"><span class="t">已应用修复 · 可回滚</span></div>
      <p class="muted small">
        已应用记录不会因重新审计而消失。发现结果不符合预期时，可在这里恢复应用前快照。
      </p>
      <div class="candidates">
        <div v-for="candidate in appliedPatches" :key="candidate.patch_id" class="candidate">
          <div class="c-head">
            <span class="src applied">已应用</span>
            <span v-if="candidate.issue_id" class="mono">{{ candidate.issue_id }}</span>
            <span class="ops mono">{{ opSummary(candidate.ops) }}</span>
          </div>
          <p v-if="candidate.rationale" class="rationale">{{ candidate.rationale }}</p>
          <div class="c-actions">
            <button class="ghost sm" @click="rollback(candidate)">人工确认并回滚</button>
            <span v-if="patchState[candidate.patch_id]?.note" class="muted tiny">
              {{ patchState[candidate.patch_id].note }}
            </span>
          </div>
        </div>
      </div>
      <button
        v-if="nextAppliedPatchesCursor"
        class="ghost load-more"
        :disabled="loadingMoreAppliedPatches"
        @click="loadAppliedPatches(true)"
      >
        {{ loadingMoreAppliedPatches ? "加载中…" : "加载更多已应用修复" }}
      </button>
    </div>

    <div class="pane contra">
      <div class="contra-head">
        <div class="section"><span class="t">防吃书 · 设定矛盾检测</span></div>
        <button
          class="ghost"
          :disabled="contraScanning || Boolean(contraValidationError)"
          @click="scanContradictions"
        >
          {{
            contraScanning
              ? "扫描中…"
              : contraUseRealJudge && llmReady
                ? "用真实模型扫描矛盾"
                : "$0 扫描设定矛盾"
          }}
        </button>
      </div>
      <p class="muted small">
        规则审计看不出「语义矛盾」——比如同一对势力在不同地方分别写成盟友和死敌。默认只做本地语义召回并把候选交给你复查，模型成本为 $0；只有明确开启下方开关后，才会让真实模型逐对判定。
      </p>
      <div class="contra-controls">
        <label class="contra-judge-toggle">
          <input
            v-model="contraUseRealJudge"
            type="checkbox"
            :disabled="!llmReady || contraScanning"
          />
          <span>
            <b>真实模型判定</b>
            <small>开启后会调用已配置的模型并产生实际模型成本；默认关闭。</small>
          </span>
        </label>
        <p v-if="!llmReady" class="muted small setup-tip">
          尚未配置模型，此开关已禁用；当前仍可运行 $0 离线扫描。
          <RouterLink to="/settings" class="golink">前往设置接入模型</RouterLink>
        </p>
        <div class="contra-control-grid">
          <label>
            <span>语义召回阈值</span>
            <input
              v-model.number="contraSemanticThreshold"
              type="number"
              min="0"
              max="1"
              step="0.05"
              :disabled="contraScanning"
            />
            <small>0–1；越高越严格，默认 0.6。</small>
          </label>
          <label>
            <span>最多模型判定对数</span>
            <input
              v-model.number="contraMaxJudge"
              type="number"
              min="1"
              max="1000"
              step="1"
              :disabled="contraScanning"
            />
            <small>1–1000；离线模式仍发送上限，但不会调用模型。</small>
          </label>
        </div>
        <p v-if="contraValidationError" class="error small" role="alert">
          {{ contraValidationError }}
        </p>
      </div>
      <p class="muted small ingest-tip">
        新素材还没入库？先到
        <RouterLink to="/extraction" class="golink">文稿提炼</RouterLink>
        或
        <RouterLink to="/import" class="golink">表格导入</RouterLink>
        把更新材料接进来，再回到这里扫矛盾。
      </p>
      <div v-if="contraRan">
        <p class="contra-mode small" data-testid="contradiction-actual-mode">
          {{ contraActualMode }}
        </p>
        <p v-if="lastContraRequest" class="muted small">
          本次请求：语义阈值 {{ lastContraRequest.semanticThreshold.toFixed(2) }} · 最多判定
          {{ lastContraRequest.maxJudge }} 对 ·
          {{ contra.semantic_used ? "语义召回已运行" : "语义召回未运行或已降级" }} · 实际判定
          {{ contra.judged_count }} / {{ contra.candidate_count }} 对
        </p>
        <p
          class="small"
          :class="contraStatus === 'CLEAN' ? 'ok-text' : 'muted'"
          :data-semantic-status="contraStatus"
        >
          {{ SEMANTIC_STATUS_MESSAGE[contraStatus] }}
        </p>
        <div v-for="(c, k) in visibleContradictions" :key="'c' + k" class="contra-card hit">
          <div class="contra-tag">矛盾 · {{ c.subjects.join(" ↔ ") }}</div>
          <p class="contra-point">{{ c.point }}</p>
          <p v-for="(s, i) in c.statements" :key="i" class="contra-stmt mono">{{ s }}</p>
        </div>
        <button
          v-if="visibleContradictionCount < contra.contradictions.length"
          type="button"
          class="ghost sm"
          @click="visibleContradictionCount += CONTRA_PAGE_SIZE"
        >显示更多矛盾（剩余 {{ contra.contradictions.length - visibleContradictionCount }}）</button>
        <div v-for="(c, k) in visibleContraReviews" :key="'r' + k" class="contra-card review">
          <div class="contra-tag review">待人工确认</div>
          <p class="contra-point">{{ c.point }}</p>
          <p v-for="(s, i) in c.statements" :key="i" class="contra-stmt mono">{{ s }}</p>
        </div>
        <button
          v-if="visibleReviewCount < contra.review_suggested.length"
          type="button"
          class="ghost sm"
          @click="visibleReviewCount += CONTRA_PAGE_SIZE"
        >显示更多待确认项（剩余 {{ contra.review_suggested.length - visibleReviewCount }}）</button>
        <p v-if="contra.judge_skipped" class="muted small">
          还有 {{ contra.judge_skipped }} 对可疑设定超出本次判定上限，未逐一判定，已保留待人工复查。
        </p>
      </div>
    </div>

    <div v-if="ran" class="filters">
      <button v-for="f in ['all', 'error', 'warning', 'info']" :key="f" class="pill" :class="{ on: filter === f }" @click="filter = f">
        {{ f === "all" ? "全部" : SEV_LABEL[f] }}
      </button>
    </div>

    <p v-if="ran && !filtered.length" class="ok-text">这一档没有问题——干净。</p>

    <TransitionGroup name="list" tag="div" class="issues">
      <div v-for="issue in filtered" :key="issue.id" class="pane issue" :class="issue.severity">
        <div class="head">
          <span class="sev" :class="issue.severity">{{ SEV_LABEL[issue.severity] ?? issue.severity }}</span>
          <span class="ref mono" :title="issue.target_ref">{{ formatRef(issue.target_ref) }}</span>
          <button class="detail-toggle" @click="toggleDetail(issue.id)">
            技术详情 <span class="detail-caret" :class="{ open: detailOpen[issue.id] }">▾</span>
          </button>
        </div>
        <p class="msg">{{ issue.message }}</p>
        <p v-if="locationOf(issue)" class="where">📍 {{ locationOf(issue) }}</p>
        <p v-if="issue.suggested_action" class="advice">✏️ {{ issue.suggested_action }}</p>
        <div v-if="detailOpen[issue.id]" class="detail-panel">
          <span class="detail-row"><span class="detail-k">规则代码</span><span class="rule mono">{{ issue.rule_code }}</span></span>
          <span class="detail-row"><span class="detail-k">引用对象</span><span class="ref mono">{{ issue.target_ref }}</span></span>
        </div>
        <div class="issue-actions">
          <button class="ghost" :disabled="suggestState[issue.id]?.busy" @click="suggest(issue)">
            {{ suggestState[issue.id]?.busy ? "生成中…" : "生成修复建议" }}
          </button>
          <span v-if="!llmReady" class="muted tiny">无模型时给确定性修复（$0）；接入模型可加 LLM 候选</span>
          <span v-if="suggestState[issue.id]?.note" class="muted tiny">{{ suggestState[issue.id].note }}</span>
        </div>

        <div v-if="suggestState[issue.id]?.candidates.length" class="candidates">
          <div v-for="c in suggestState[issue.id].candidates" :key="c.patch_id" class="candidate">
            <div class="c-head">
              <span class="src" :class="c.source">{{ c.source === "llm" ? "模型" : "确定性" }}</span>
              <span class="resolved">解决 {{ c.resolved_error_count }} 个错误</span>
              <span class="ops mono">{{ opSummary(c.ops) }}</span>
            </div>
            <p v-if="c.rationale" class="rationale">{{ c.rationale }}</p>
            <div class="c-actions">
              <button
                v-if="!patchState[c.patch_id]?.applied"
                class="primary sm"
                @click="applyPatch(c)"
              >
                应用
              </button>
              <button v-else class="ghost sm" @click="rollback(c)">回滚</button>
              <span v-if="patchState[c.patch_id]?.note" class="muted tiny">{{ patchState[c.patch_id].note }}</span>
            </div>
          </div>
        </div>
      </div>
    </TransitionGroup>
    <div v-if="ran && issueTotal" class="pagination-bar" aria-live="polite">
      <span>已显示 {{ issues.length }} / {{ issueTotal }} 条</span>
      <button
        v-if="nextIssuesCursor"
        class="ghost"
        :disabled="loadingMoreIssues"
        @click="loadIssues(true)"
      >
        {{ loadingMoreIssues ? "加载中…" : "加载下一页" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.bar {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin-bottom: 0.6rem;
}

.op {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
  font: inherit;
  font-size: 0.88rem;
  width: 15rem;
}

.op:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
  color: #241a05;
  font-weight: 600;
  padding: 0.5rem 1rem;
  cursor: pointer;
}

button.primary.sm {
  padding: 0.32rem 0.8rem;
  font-size: 0.82rem;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

button.ghost {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.82rem;
  padding: 0.4rem 0.8rem;
  cursor: pointer;
}

button.ghost:hover:not(:disabled) {
  border-color: var(--ow-gold-soft);
  color: var(--ow-ink);
}

button.ghost.sm {
  padding: 0.32rem 0.8rem;
}

.chips {
  display: flex;
  gap: 0.4rem;
}

.chip {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  background: rgba(16, 22, 48, 0.6);
  color: var(--ow-muted);
  font-size: 0.78rem;
  padding: 0.16rem 0.65rem;
}

.chip b {
  color: var(--ow-ink);
}

.chip.red {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

.chip.amber {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.error {
  color: #e89a9a;
}

.ok-text {
  color: #8ed4ac;
}

.contra {
  margin: 0.7rem 0;
  padding: 0.8rem 1rem;
}
.contra-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.6rem;
}
.contra .small {
  font-size: 0.8rem;
  margin: 0.2rem 0 0.5rem;
}
.ingest-tip {
  padding: 0.35rem 0.6rem;
  border-left: 2px solid var(--ow-gold-soft);
  background: rgba(240, 210, 138, 0.05);
}
.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}
.contra-card {
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  background: var(--ow-panel-2);
  padding: 0.55rem 0.75rem;
  margin-top: 0.5rem;
}
.contra-card.hit {
  border-color: rgba(224, 133, 133, 0.5);
}
.contra-tag {
  font-size: 0.78rem;
  color: #e89a9a;
  font-weight: 600;
}
.contra-tag.review {
  color: var(--ow-gold-bright);
}
.contra-point {
  margin: 0.25rem 0;
  font-size: 0.86rem;
  color: var(--ow-ink);
}
.contra-stmt {
  font-size: 0.78rem;
  color: var(--ow-cyan);
  margin: 0.1rem 0;
}

.filters {
  display: flex;
  gap: 0.4rem;
  margin: 0.5rem 0 0.8rem;
}

.pill {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  background: transparent;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.8rem;
  padding: 0.2rem 0.7rem;
  cursor: pointer;
}

.pill.on {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
}

.issues {
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.issue {
  padding: 0.7rem 1rem;
  border-left: 3px solid var(--ow-line);
}

.issue.error {
  border-left-color: rgba(224, 133, 133, 0.7);
}

.issue.warn,
.issue.warning {
  border-left-color: rgba(224, 180, 106, 0.7);
}

.issue.info {
  border-left-color: rgba(143, 214, 232, 0.6);
}

.head {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  flex-wrap: wrap;
}

.detail-toggle {
  background: transparent;
  border: none;
  border-radius: 0;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.74rem;
  padding: 0;
  cursor: pointer;
  margin-left: auto;
  opacity: 0.7;
}

.detail-toggle:hover {
  color: var(--ow-ink);
  opacity: 1;
}

.detail-caret {
  display: inline-block;
  transition: transform 0.2s ease;
  font-size: 0.7rem;
}

.detail-caret.open {
  transform: rotate(180deg);
}

.detail-panel {
  background: rgba(10, 14, 36, 0.35);
  box-shadow: inset 0 1px 0 rgba(240, 210, 138, 0.07);
  border-radius: 3px;
  padding: 0.45rem 0.65rem;
  margin-top: 0.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.detail-row {
  display: flex;
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.78rem;
}

.detail-k {
  font-size: 0.72rem;
  color: var(--ow-muted);
  min-width: 4rem;
}

.sev {
  font-size: 0.74rem;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.08rem 0.55rem;
  border: 1px solid var(--ow-line);
}

.sev.error {
  color: #e89a9a;
  border-color: rgba(224, 133, 133, 0.45);
}

.sev.warn,
.sev.warning {
  color: #e6c07e;
  border-color: rgba(224, 180, 106, 0.45);
}

.sev.info {
  color: var(--ow-cyan);
  border-color: rgba(143, 214, 232, 0.4);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.76rem;
}

.rule {
  color: var(--ow-gold-bright);
}

.ref {
  color: var(--ow-cyan);
}

.msg {
  margin: 0.4rem 0 0.5rem;
  font-size: 0.88rem;
  line-height: 1.6;
}

.where,
.advice {
  margin: 0 0 0.35rem;
  font-size: 0.82rem;
  line-height: 1.55;
}

.where {
  color: var(--fg-muted, #8a8f98);
  font-family: var(--font-mono, ui-monospace, monospace);
}

.advice {
  padding: 0.4rem 0.6rem;
  border-left: 2px solid var(--accent, #6ea8fe);
  background: var(--bg-subtle, rgba(110, 168, 254, 0.08));
  border-radius: 0 4px 4px 0;
}

.issue-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.tiny {
  font-size: 0.74rem;
}
.contra-controls {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
  margin: 0.65rem 0;
  padding: 0.7rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: rgba(10, 14, 36, 0.28);
}
.contra-judge-toggle {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  color: var(--ow-ink);
}
.contra-judge-toggle input {
  margin-top: 0.2rem;
}
.contra-judge-toggle span {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.contra-judge-toggle small,
.contra-control-grid small {
  color: var(--ow-muted);
  font-size: 0.74rem;
}
.contra-control-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.65rem;
}
.contra-control-grid label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  color: var(--ow-muted);
  font-size: 0.8rem;
}
.contra-control-grid input {
  width: 100%;
  box-sizing: border-box;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-panel-2);
  color: var(--ow-ink);
  padding: 0.42rem 0.55rem;
  font: inherit;
}
.setup-tip {
  margin-left: 1.4rem !important;
}
.contra-mode {
  color: var(--ow-cyan);
  font-weight: 600;
}

.proposal-inbox {
  margin: 0.75rem 0;
  padding: 0.85rem 1rem;
  border-color: var(--ow-gold-soft);
}

.candidates {
  margin-top: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.candidate {
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.55rem 0.75rem;
}

.c-head {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  flex-wrap: wrap;
}

.src {
  font-size: 0.72rem;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.06rem 0.5rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-muted);
}

.src.llm {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
}

.resolved {
  font-size: 0.78rem;
  color: #8ed4ac;
}

.ops {
  color: var(--ow-muted);
}

.rationale {
  margin: 0.35rem 0;
  font-size: 0.83rem;
  line-height: 1.55;
  color: var(--ow-ink);
}

.c-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.list-enter-active,
.list-leave-active,
.list-move {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.list-leave-to {
  opacity: 0;
}
.pagination-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 0.8rem;
  margin-top: 1rem;
  color: var(--ow-muted);
  font-size: 0.82rem;
}

@media (max-width: 600px) {
  .bar { align-items: stretch; flex-direction: column; }
  .bar .op,
  .bar button { width: 100%; min-height: 44px; }
  .c-actions { align-items: stretch; flex-direction: column; }
  .c-actions button { width: 100%; }
  .contra-head { align-items: stretch; flex-direction: column; }
  .contra-head button { width: 100%; min-height: 44px; }
  .contra-control-grid { grid-template-columns: 1fr; }
}
</style>
