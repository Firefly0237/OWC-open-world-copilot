<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, toRefs } from "vue";
import PageHead from "../components/PageHead.vue";
import StepperProgress from "../components/StepperProgress.vue";
import { apiGet, llmConfig, llmParams } from "../api";
import { getJobChannel, resumeJob, startJob } from "../jobs";

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "orchestrate", label: "协作" },
  { key: "complete", label: "核验" },
];

const FLAVORS: Record<string, string> = {
  accepted: "建立多智能体会话与持久化黑板…",
  orchestrate: "编排者正在分派诊断、修复与独立核验任务…",
  complete: "汇总可追溯的协作结论。",
};

interface FlowRow {
  msg_type: string;
  from_agent: string;
  to_agent: string;
  status: string;
}

interface WorkerSummary {
  agent_id?: string;
  role?: string;
  stop_reason?: string;
  final_answer?: string;
  [key: string]: unknown;
}

interface VerifierVerdict {
  agent_id?: string;
  target_agent?: string;
  verdict?: string;
  rationale?: string;
  [key: string]: unknown;
}

interface RepairProposal {
  patch_id: string;
  issue_id?: string | null;
  rationale?: string;
  ops?: Record<string, unknown>[];
}

interface MultiAgentResult {
  session_id: string;
  goal: string;
  participants: string[];
  blackboard_flow: FlowRow[];
  worker_summaries: WorkerSummary[];
  verifier_verdicts: VerifierVerdict[];
  proposals?: RepairProposal[];
  synthesis: string;
  decomposition_degraded: boolean;
  thread_id: string;
  blackboard_storage: string;
  budget_scope: {
    per_agent_max_tool_calls?: number | null;
    max_decomposed_subtasks?: number;
    session_global_budget_status?: string;
    max_cost_usd?: string;
    session_global_model_budget?: {
      model_calls_limit?: number | null;
      model_calls_used?: number | null;
      model_calls_remaining?: number | null;
      settled_cost_limit_usd?: number | null;
      settled_cost_usd?: number;
      cost_is_estimate?: boolean;
      cost_gate_exhausted?: boolean;
      cost_gate_semantics?: string;
    };
    session_global_tool_budget?: {
      tool_calls_limit?: number | null;
      tool_calls_used?: number | null;
      tool_calls_remaining?: number | null;
    };
  };
  telemetry?: { call_count?: number; total_cost_usd?: number };
}

const goal = ref("");
const llmReady = ref(llmConfig().ready);
const offlineAllowed = ref(false);
const runtimeChecked = ref(false);
const runMode = ref<"offline" | "real">("real");
const maxModelCalls = ref<number | "">(128);
const maxToolCalls = ref<number | "">(6);
const maxTotalToolCalls = ref<number | "">(256);
const maxCostUsd = ref<number | "">("");
const validModelLimit = computed(() => {
  const value = Number(maxModelCalls.value);
  return Number.isInteger(value) && value >= 1 && value <= 512;
});
const validToolLimit = computed(() => {
  const value = Number(maxToolCalls.value);
  return Number.isInteger(value) && value >= 1 && value <= 100;
});
const validTotalToolLimit = computed(() => {
  const value = Number(maxTotalToolCalls.value);
  return Number.isInteger(value) && value >= 1 && value <= 4096;
});
const validCostThreshold = computed(() => {
  if (maxCostUsd.value === "") return true;
  const value = Number(maxCostUsd.value);
  return Number.isFinite(value) && value >= 0;
});
const runModeReady = computed(() =>
  runMode.value === "offline" ? offlineAllowed.value : llmReady.value,
);
const job = getJobChannel<MultiAgentResult>("multi_agent", STAGES);
const { running, stageIndex, elapsed, result } = toRefs(job);

function onEvent(
  channel: { stageIndex: number; hint: string },
  event: { type: string; data: Record<string, unknown> },
): void {
  if (event.type !== "stage") return;
  const stage = String(event.data.stage ?? "");
  const index = STAGES.findIndex((item) => item.key === stage);
  if (index >= 0) channel.stageIndex = index;
  if (stage === "orchestrate") channel.hint = "诊断、修复提案与独立验证正在通过黑板交接";
}

const parseResult = (raw: Record<string, unknown>): MultiAgentResult =>
  raw as unknown as MultiAgentResult;

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
  if (llmReady.value) runMode.value = "real";
  else runMode.value = offlineAllowed.value ? "offline" : "real";
}

async function loadRuntimeCapabilities(): Promise<void> {
  try {
    const health = await apiGet<{ offline_llm_allowed?: boolean }>("/health");
    offlineAllowed.value = health.offline_llm_allowed === true;
  } catch {
    // Fail closed: test doubles must only appear when the server advertises them explicitly.
    offlineAllowed.value = false;
  } finally {
    runtimeChecked.value = true;
    onLlmChanged();
  }
}

onMounted(() => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  void loadRuntimeCapabilities();
  void resumeJob<MultiAgentResult>("multi_agent", {
    stages: STAGES,
    onEvent,
    parseResult,
  });
});
onUnmounted(() => window.removeEventListener("ow-llm-changed", onLlmChanged));

async function run(): Promise<void> {
  if (
    !goal.value.trim() ||
    running.value ||
    !validModelLimit.value ||
    !validToolLimit.value ||
    !validTotalToolLimit.value ||
    !validCostThreshold.value ||
    !runModeReady.value
  ) return;
  const modelLimit = Number(maxModelCalls.value);
  const toolLimit = Number(maxToolCalls.value);
  const totalToolLimit = Number(maxTotalToolCalls.value);
  const costThreshold = maxCostUsd.value === "" ? null : Number(maxCostUsd.value);
  await startJob<MultiAgentResult>("multi_agent", {
    kind: "multi_agent",
    stages: STAGES,
    params: {
      goal: goal.value.trim(),
      max_model_calls: modelLimit,
      max_tool_calls: toolLimit,
      max_total_tool_calls: totalToolLimit,
      ...(costThreshold == null ? {} : { max_cost_usd: costThreshold }),
      ...(runMode.value === "offline" ? { llm_mode: "offline" } : llmParams()),
    },
    onEvent,
    parseResult,
  });
}

function pretty(value: unknown): string {
  return JSON.stringify(value, null, 2);
}
</script>

<template>
  <section>
    <PageHead
      overline="MULTI-AGENT"
      title="多智能体协作"
      purpose="让编排者、诊断者、修复者和独立核验者通过持久化黑板完成一次可追溯协作。"
    />

    <div class="pane form">
      <label class="field">
        <span>协作目标</span>
        <textarea
          v-model="goal"
          rows="4"
          maxlength="1200"
          placeholder="例如：检查主线任务链的断链和时序冲突，提出可审阅的修复方案并独立核验"
        ></textarea>
      </label>
      <div class="controls">
        <label class="field compact">
          <span>运行模式</span>
          <select v-model="runMode">
            <option v-if="offlineAllowed" value="offline">离线测试（开发 / CI · $0）</option>
            <option value="real" :disabled="!llmReady">真实模型</option>
          </select>
        </label>
        <label class="field compact">
          <span>会话模型调用硬上限</span>
          <input v-model.number="maxModelCalls" type="number" min="1" max="512" />
        </label>
        <label class="field compact">
          <span>每个子任务的工具上限</span>
          <input v-model.number="maxToolCalls" type="number" min="1" max="100" />
        </label>
        <label class="field compact">
          <span>会话工具调用硬上限</span>
          <input v-model.number="maxTotalToolCalls" type="number" min="1" max="4096" />
        </label>
        <label class="field compact">
          <span>会话成本门槛（结算后阻止下一次派发）</span>
          <input v-model.number="maxCostUsd" type="number" min="0" step="0.01" placeholder="不限" />
        </label>
      </div>
      <p class="muted small">
        修复智能体只能生成提案；正典写入仍需人工确认。模型调用与会话工具调用各有一个全局硬账本；“每个子任务的工具上限”另行限制单次执行，最多分解 6 个任务。成本门槛依据已结算成本阻止下一次派发，不会中断正在执行的请求，因此最后一次调用结算后可能略超门槛。
      </p>
      <p v-if="runMode === 'offline'" class="muted small">
        离线测试仍会执行编排、并发工作者、SQLite 黑板、独立核验和提案入库，只用确定性测试替身替换推理模型；该选项仅在服务端显式启用开发 / CI 模式时出现。
      </p>
      <p v-else-if="runtimeChecked && !llmReady" class="muted small">
        当前未配置真实模型，且服务端未开放离线测试。请先到设置页完成模型连接。
      </p>
      <button
        class="primary"
        :disabled="running || !goal.trim() || !validModelLimit || !validToolLimit || !validTotalToolLimit || !validCostThreshold || !runModeReady"
        @click="run"
      >
        {{ running ? "协作中…" : "开始协作" }}
      </button>
    </div>

    <StepperProgress
      v-if="running || stageIndex >= 0"
      :stages="STAGES"
      :index="stageIndex"
      :running="running"
      :elapsed="elapsed"
      :hint="job.hint"
      :flavors="FLAVORS"
    />

    <div v-if="result" class="results">
      <div v-if="result.decomposition_degraded" class="pane notice">
        当前模型没有返回可解析的动态分工，系统已明确降级到内置诊断/修复拓扑；协作与核验仍真实执行。
      </div>

      <div class="pane section">
        <div class="chips">
          <span v-for="participant in result.participants" :key="participant" class="chip mono">
            {{ participant }}
          </span>
          <span class="chip">黑板 {{ result.blackboard_storage === "persistent_sqlite" ? "已持久化" : "内存" }}</span>
          <span class="chip">每任务工具上限 {{ result.budget_scope.per_agent_max_tool_calls ?? "不限" }}</span>
          <span class="chip">分解任务上限 {{ result.budget_scope.max_decomposed_subtasks ?? 6 }}</span>
          <span class="chip">
            会话模型调用
            {{ result.budget_scope.session_global_model_budget?.model_calls_used ?? "?" }} /
            {{ result.budget_scope.session_global_model_budget?.model_calls_limit ?? "不限" }}
          </span>
          <span class="chip">
            会话工具调用
            {{ result.budget_scope.session_global_tool_budget?.tool_calls_used ?? "?" }} /
            {{ result.budget_scope.session_global_tool_budget?.tool_calls_limit ?? "不限" }}
          </span>
          <span
            v-if="result.budget_scope.session_global_model_budget?.settled_cost_limit_usd != null"
            class="chip"
          >
            已结算成本 ${{ (result.budget_scope.session_global_model_budget?.settled_cost_usd ?? 0).toFixed(4) }} /
            ${{ result.budget_scope.session_global_model_budget?.settled_cost_limit_usd?.toFixed(4) }}
          </span>
          <span
            v-if="result.budget_scope.session_global_model_budget?.cost_gate_exhausted"
            class="chip budget-stop"
          >成本门槛已阻止继续派发</span>
          <span v-if="job.cost" class="chip">本次 ${{ job.cost.toFixed(4) }}</span>
        </div>
        <h2>最终综合结论</h2>
        <pre class="synthesis">{{ result.synthesis }}</pre>
      </div>

      <div v-if="result.proposals?.length" class="pane section proposal-result">
        <h2>已生成的修复提案</h2>
        <p class="muted small">
          这些提案已结构化存入运行库，但尚未写入正典。请到校勘修复页逐条人工批准。
        </p>
        <div class="proposal-list">
          <div v-for="proposal in result.proposals" :key="proposal.patch_id" class="proposal-row">
            <b class="mono">{{ proposal.patch_id }}</b>
            <span v-if="proposal.issue_id" class="muted">问题 {{ proposal.issue_id }}</span>
            <span>{{ proposal.rationale || `${proposal.ops?.length ?? 0} 个变更操作` }}</span>
          </div>
        </div>
        <RouterLink class="review-link" to="/audit">去「校勘修复」人工处置</RouterLink>
      </div>

      <div class="pane section">
        <h2>黑板消息流</h2>
        <p class="muted small">每一行都是一次有归属的 agent 间交接，顺序来自服务端持久化记录。</p>
        <div class="flow" role="table" aria-label="多智能体黑板消息流">
          <div class="flow-row flow-head" role="row">
            <span role="columnheader">类型</span><span role="columnheader">发送者</span>
            <span role="columnheader">接收者</span><span role="columnheader">状态</span>
          </div>
          <div v-for="(row, index) in result.blackboard_flow" :key="index" class="flow-row" role="row">
            <b class="mono" role="cell">{{ row.msg_type }}</b>
            <span role="cell">{{ row.from_agent }}</span>
            <span class="arrow" aria-hidden="true">→</span>
            <span role="cell">{{ row.to_agent }}</span>
            <span role="cell">{{ row.status }}</span>
          </div>
        </div>
      </div>

      <div class="two-column">
        <div class="pane section">
          <h2>执行者结果</h2>
          <details v-for="(summary, index) in result.worker_summaries" :key="index" open>
            <summary>
              {{ summary.agent_id ?? summary.role ?? `执行者 ${index + 1}` }}
              <span v-if="summary.stop_reason" class="muted">· {{ summary.stop_reason }}</span>
            </summary>
            <pre>{{ pretty(summary) }}</pre>
          </details>
        </div>
        <div class="pane section">
          <h2>独立核验</h2>
          <details v-for="(verdict, index) in result.verifier_verdicts" :key="index" open>
            <summary>
              {{ verdict.agent_id ?? "核验者" }}
              <span v-if="verdict.verdict" class="verdict">· {{ verdict.verdict }}</span>
            </summary>
            <pre>{{ pretty(verdict) }}</pre>
          </details>
        </div>
      </div>
    </div>
  </section>
</template>

<style scoped>
.form, .section { padding: 1rem 1.15rem; }
.form { display: flex; flex-direction: column; gap: 0.85rem; }
.field { display: flex; flex-direction: column; gap: 0.35rem; color: var(--ow-muted); font-size: 0.82rem; }
.controls { display: flex; flex-wrap: wrap; gap: 0.8rem; }
.compact { width: min(100%, 16rem); }
textarea, input, select {
  min-height: 42px;
  padding: 0.55rem 0.7rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  background: var(--ow-panel-2);
  font: inherit;
}
textarea { resize: vertical; }
textarea:focus, input:focus, select:focus { outline: none; border-color: var(--ow-gold-soft); }
.primary {
  min-height: 44px;
  align-self: flex-start;
  padding: 0.55rem 1rem;
  border: 1px solid rgba(240, 210, 138, 0.65);
  color: #241a05;
  background: linear-gradient(180deg, #f0d28a, #b9924a);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}
.primary:disabled { opacity: 0.55; cursor: not-allowed; }
.results { display: grid; gap: 0.9rem; margin-top: 0.9rem; }
.notice { padding: 0.8rem 1rem; border-color: rgba(224, 180, 106, 0.55); color: #e6c07e; }
.section h2 { margin: 0.8rem 0 0.55rem; color: var(--ow-gold-bright); font-size: 0.96rem; }
.chips { display: flex; flex-wrap: wrap; gap: 0.4rem; }
.chip { padding: 0.18rem 0.55rem; border: 1px solid var(--ow-line); color: var(--ow-muted); font-size: 0.76rem; }
.budget-stop { border-color: var(--ow-cinnabar-soft); color: #efae8e; }
.mono { color: var(--ow-cyan); font-family: ui-monospace, Consolas, monospace; }
.small { font-size: 0.78rem; }
pre { max-width: 100%; overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere; color: var(--ow-ink-soft); font: 0.76rem/1.6 ui-monospace, Consolas, monospace; }
.synthesis { margin: 0; color: var(--ow-ink); font-family: inherit; font-size: 0.87rem; }
.flow { display: grid; gap: 1px; border: 1px solid var(--ow-line); background: var(--ow-line); }
.flow-row { position: relative; display: grid; grid-template-columns: 1.15fr 1fr 1fr 0.75fr; gap: 0.55rem; padding: 0.5rem 0.65rem; background: var(--ow-panel-2); font-size: 0.8rem; }
.flow-head { color: var(--ow-muted); font-size: 0.72rem; }
.arrow { position: absolute; left: 52%; color: var(--ow-muted); transform: translateX(-50%); }
.two-column { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.9rem; }
details { padding: 0.55rem 0; border-top: 1px solid var(--ow-line); }
summary { min-height: 36px; cursor: pointer; color: var(--ow-ink); font-size: 0.84rem; }
.verdict { color: var(--ow-gold-bright); }
.proposal-result { border-color: var(--ow-gold-soft); }
.proposal-list { display: grid; gap: 0.4rem; }
.proposal-row { display: flex; flex-wrap: wrap; gap: 0.55rem; padding: 0.5rem 0.6rem; border: 1px solid var(--ow-line); background: var(--ow-panel-2); font-size: 0.8rem; }
.review-link { display: inline-flex; min-height: 40px; align-items: center; margin-top: 0.7rem; color: var(--ow-gold-bright); }
@media (max-width: 760px) {
  .two-column { grid-template-columns: 1fr; }
  .compact { width: 100%; }
  .primary { width: 100%; }
  .flow-head { display: none; }
  .flow-row { grid-template-columns: 1fr 1fr; min-height: 86px; }
  .flow-row > :nth-child(4), .flow-row > :nth-child(5) { grid-column: 2; }
  .arrow { display: none; }
}
</style>
