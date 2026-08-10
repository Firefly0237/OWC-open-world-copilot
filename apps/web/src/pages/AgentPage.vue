<script setup lang="ts">
/**
 * 智能体诊断 (E1) — the ReAct diagnosis run inside the product surface.
 *
 * It is the seventh async job kind, driven through the SAME `startJob` channel every other
 * long-running page uses (submit → SSE progress → terminal result); nothing about the transport
 * is specific to the agent. The one agent-specific interaction is the human-approval gate: a run
 * that pauses ends its job with `status="interrupted"`, and the human's 批准 / 拒绝 is simply the
 * NEXT job on the same channel (`resume` + `decision` + the paused run's `thread_id`).
 *
 * The agent reads and PROPOSES; it can never write canon. This page therefore offers no write
 * control at all — approving a gate authorises one propose-only tool call, and the proposal still
 * has to be accepted by a human in 校勘修复.
 */
import { computed, onMounted, onUnmounted, ref, toRefs } from "vue";
import PageHead from "../components/PageHead.vue";
import StepperProgress from "../components/StepperProgress.vue";
import { apiGet, llmConfig, llmParams } from "../api";
import { getJobChannel, resumeJob, startJob } from "../jobs";

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "recall", label: "调取记忆" },
  { key: "reason", label: "推理取证" },
  { key: "persist", label: "归档" },
];

const FLAVORS: Record<string, string> = {
  accepted: "接下诊断任务…",
  recall: "翻阅以往的诊断经验…",
  reason: "调用确定性工具逐项取证…",
  persist: "留档本次诊断轨迹。",
};

interface AuditIssue {
  id?: string;
  rule_code?: string;
  severity?: string;
  message?: string;
  target_ref?: string;
  source_path?: string;
  source_line?: number;
  suggested_action?: string;
}

interface AgentStep {
  thought: string;
  action: string;
  action_input: Record<string, unknown>;
  observation: string;
  result: Record<string, unknown> | null;
  is_error: boolean;
  observation_chars: number;
  is_truncated: boolean;
}

interface PendingAction {
  name: string;
  args?: Record<string, unknown>;
  description?: string;
}

interface AgentResult {
  goal: string;
  final_answer: string;
  stop_reason: string;
  steps: AgentStep[];
  step_count: number;
  interrupted: boolean;
  interrupt_request: { action_requests?: PendingAction[] } | null;
  status: string;
  thread_id: string;
  skills: string[];
  unknown_skills?: string[];
  tool_transport: "native" | "mcp";
  mcp_traces?: {
    tool_name?: string;
    status?: string;
    duration_ms?: number;
    argument_summary?: Record<string, unknown>;
    risk_flags?: string[];
  }[];
  run_budget?: {
    total_model_call_limit?: number;
    tool_call_limit?: number;
    max_cost_usd?: number | null;
    total_model_calls_reserved?: number;
    settled_cost_usd?: number;
  };
  telemetry?: { call_count?: number; total_cost_usd?: number };
  agent_run_id?: string | null;
}

const goal = ref("");
const llmReady = ref(llmConfig().ready);
const offlineAllowed = ref(false);
const runtimeChecked = ref(false);
const runMode = ref<"offline" | "real">("real");
const maxSteps = ref<number | "">(6);
const hitl = ref(true);
const advanced = ref(false);
const skillsText = ref("");
const recall = ref(true);
const toolTransport = ref<"native" | "mcp">("native");
const maxModelCalls = ref<number | "">(8);
const maxToolCalls = ref<number | "">(6);
const maxCostUsd = ref<number | "">("");
const earlyStopping = ref<"force" | "generate">("force");
const rejectReason = ref("");
// the thread of the run currently awaiting a decision; captured before a resume job clears
// the channel result, because the resume needs it to find the paused checkpoint.
const pendingThreadId = ref("");

const job = getJobChannel<AgentResult>("agent_diagnose", STAGES);
const { running, stageIndex, elapsed, result } = toRefs(job);

const paused = computed(() => result.value?.status === "interrupted");
const pendingActions = computed<PendingAction[]>(
  () => result.value?.interrupt_request?.action_requests ?? [],
);
const controlsValid = computed(() => {
  const validInt = (value: number | "", max: number): boolean =>
    value !== "" && Number.isInteger(Number(value)) && Number(value) >= 1 && Number(value) <= max;
  const cost = maxCostUsd.value;
  return (
    validInt(maxSteps.value, 20) &&
    validInt(maxModelCalls.value, 100) &&
    validInt(maxToolCalls.value, 100) &&
    (cost === "" || (Number.isFinite(Number(cost)) && Number(cost) >= 0))
  );
});
const runModeReady = computed(() =>
  runMode.value === "offline" ? offlineAllowed.value : llmReady.value,
);
/** Audit issues the deterministic tools actually returned — the grounded half of the report. */
const findings = computed<AuditIssue[]>(() => {
  const rows: AuditIssue[] = [];
  for (const step of result.value?.steps ?? []) {
    const issues = step.result?.issues;
    if (Array.isArray(issues)) rows.push(...(issues as AuditIssue[]));
  }
  return rows;
});

function onEvent(
  ch: { stageIndex: number; hint: string },
  event: { type: string; data: Record<string, unknown> },
): void {
  if (event.type !== "stage") return;
  const stage = String(event.data.stage ?? "");
  const index = STAGES.findIndex((s) => s.key === stage);
  if (index >= 0) ch.stageIndex = index;
  if (stage === "reason") ch.hint = `步数上限 ${String(event.data.max_steps ?? "")}`;
}

const parseAgentResult = (raw: Record<string, unknown>): AgentResult =>
  raw as unknown as AgentResult;

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
    // Fail closed: a missing capability response must never expose the test double as product UI.
    offlineAllowed.value = false;
  } finally {
    runtimeChecked.value = true;
    onLlmChanged();
  }
}

onMounted(() => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  void loadRuntimeCapabilities();
  void resumeJob<AgentResult>("agent_diagnose", {
    stages: STAGES,
    onEvent,
    parseResult: parseAgentResult,
  });
});
onUnmounted(() => window.removeEventListener("ow-llm-changed", onLlmChanged));

async function submit(params: Record<string, unknown>): Promise<void> {
  await startJob<AgentResult>("agent_diagnose", {
    kind: "agent_diagnose",
    stages: STAGES,
    params: {
      ...params,
      ...(runMode.value === "offline" ? { llm_mode: "offline" } : llmParams()),
    },
    onEvent,
    parseResult: parseAgentResult,
  });
}

async function run(): Promise<void> {
  if (!goal.value.trim() || running.value || !controlsValid.value || !runModeReady.value) return;
  pendingThreadId.value = "";
  rejectReason.value = "";
  const skills = skillsText.value.split(",").map((item) => item.trim()).filter(Boolean);
  await submit({
    goal: goal.value.trim(),
    max_steps: Number(maxSteps.value),
    hitl: hitl.value,
    recall: recall.value,
    tool_transport: toolTransport.value,
    early_stopping: earlyStopping.value,
    max_model_calls: Number(maxModelCalls.value),
    max_tool_calls: Number(maxToolCalls.value),
    ...(maxCostUsd.value === "" ? {} : { max_cost_usd: Number(maxCostUsd.value) }),
    ...(skills.length ? { skills } : {}),
  });
}

/** 批准 / 拒绝 the paused gate: a resume job on the same channel, no separate protocol. */
async function decide(decision: "approve" | "reject"): Promise<void> {
  const thread = result.value?.thread_id ?? pendingThreadId.value;
  if (!thread || running.value) return;
  pendingThreadId.value = thread;
  await submit({
    thread_id: thread,
    resume: true,
    decision,
    ...(decision === "reject" && rejectReason.value.trim()
      ? { decision_message: rejectReason.value.trim() }
      : {}),
  });
}
</script>

<template>
  <section>
    <PageHead
      overline="AGENT"
      title="智能体诊断"
      purpose="交给 agent 一个诊断目标，它用确定性工具逐项取证并给出结论。"
    />

    <div class="pane form">
      <label class="field">
        <span class="label">要诊断什么</span>
        <textarea
          v-model="goal"
          rows="3"
          maxlength="800"
          placeholder="例如：检查主线任务链是否有断链和时序矛盾，并给出下一步"
        ></textarea>
      </label>
      <div class="row">
        <label class="field narrow">
          <span class="label">运行模式</span>
          <select v-model="runMode">
            <option v-if="offlineAllowed" value="offline">离线测试（开发 / CI · $0）</option>
            <option value="real" :disabled="!llmReady">真实模型</option>
          </select>
        </label>
        <label class="field narrow">
          <span class="label">规划步数上限</span>
          <input v-model.number="maxSteps" type="number" min="1" max="20" />
        </label>
        <label class="check">
          <input v-model="hitl" type="checkbox" />
          <span>敏感动作先暂停等我批准（agent 提出修复提案前会停下来）</span>
        </label>
      </div>
      <p class="muted small">
        agent 全程只读与提议：它能审计、检索、提修复方案；提案统一到「校勘修复」人工处置。
      </p>
      <button type="button" class="advanced-toggle" :aria-expanded="advanced" @click="advanced = !advanced">
        高级运行参数 <span aria-hidden="true">{{ advanced ? "−" : "+" }}</span>
      </button>
      <div v-if="advanced" class="advanced-grid">
        <label class="field">
          <span class="label">工具传输</span>
          <select v-model="toolTransport">
            <option value="native">Native（进程内）</option>
            <option value="mcp">MCP（仅 audit_project，真实 stdio）</option>
          </select>
        </label>
        <label class="field">
          <span class="label">模型调用总上限</span>
          <input v-model.number="maxModelCalls" type="number" min="1" max="100" />
        </label>
        <label class="field">
          <span class="label">工具调用总上限</span>
          <input v-model.number="maxToolCalls" type="number" min="1" max="100" />
        </label>
        <label class="field">
          <span class="label">美元预算（留空为不限）</span>
          <input v-model.number="maxCostUsd" type="number" min="0" step="0.01" placeholder="不限" />
        </label>
        <label class="field">
          <span class="label">步数耗尽策略</span>
          <select v-model="earlyStopping">
            <option value="force">立即停止</option>
            <option value="generate">再生成一次总结</option>
          </select>
        </label>
        <label class="field span-two">
          <span class="label">技能白名单（逗号分隔，留空为全部）</span>
          <input v-model="skillsText" type="text" placeholder="audit_project, retrieve_context" />
        </label>
        <label class="check span-two">
          <input v-model="recall" type="checkbox" />
          <span>调取相似历史运行与经验记忆</span>
        </label>
      </div>
      <p v-if="!controlsValid" class="error small">
        运行上限必须是允许范围内的整数；美元预算可留空，否则须为非负数字。
      </p>
      <p v-if="runMode === 'offline'" class="muted small">
        开发 / CI 测试通道已由服务端显式开启：编排、工具、检查点与人工门控照常运行，只把推理模型替换为确定性 $0 测试模型；此模式不是产品输出。
      </p>
      <p v-else-if="runtimeChecked && !llmReady" class="muted small">
        当前服务未开放测试模型。请先到 <RouterLink to="/settings">设置</RouterLink> 接入真实模型。
      </p>
      <button class="primary" :disabled="running || !goal.trim() || !controlsValid || !runModeReady" @click="run">
        {{ running ? "诊断中…" : "开始诊断" }}
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

    <div v-if="paused" class="pane gate">
      <h2 class="gate-title">等待你的批准</h2>
      <p class="muted small">
        agent 想执行下面这一步。它属于「提议类」动作——批准后产出的仍然是待审提案，不会写入正典。
      </p>
      <div v-for="(action, i) in pendingActions" :key="i" class="pending">
        <span class="badge">待批准</span>
        <b class="mono">{{ action.name }}</b>
        <span class="muted args mono">{{ JSON.stringify(action.args ?? {}) }}</span>
      </div>
      <label class="field">
        <span class="label">拒绝理由（可选，会回传给模型）</span>
        <input v-model="rejectReason" maxlength="200" placeholder="例如：这条先不动，等策划确认" />
      </label>
      <div class="gate-actions">
        <button class="primary" :disabled="running" @click="decide('approve')">批准并继续</button>
        <button class="ghost" :disabled="running" @click="decide('reject')">拒绝这一步</button>
      </div>
    </div>

    <div v-else-if="result" class="pane done">
      <div class="chips">
        <span class="chip">工具步数 <b>{{ result.step_count }}</b></span>
        <span class="chip" :class="{ amber: result.stop_reason !== 'finished' }">
          结束方式 <b>{{ result.stop_reason }}</b>
        </span>
        <span class="chip red" v-if="findings.length">审计发现 <b>{{ findings.length }}</b></span>
        <span v-if="job.cost" class="chip">本次 <b>${{ job.cost.toFixed(4) }}</b></span>
        <span class="chip">
          审计工具传输 <b>{{ result.tool_transport === "mcp" ? "MCP stdio" : "Native" }}</b>
        </span>
        <span v-if="result.agent_run_id" class="chip mono">Run <b>{{ result.agent_run_id }}</b></span>
      </div>
      <p v-if="result.unknown_skills?.length" class="warn">
        这些技能名不存在，已忽略：{{ result.unknown_skills.join("、") }}
      </p>

      <h2 class="section-title">诊断结论</h2>
      <p class="answer">{{ result.final_answer || "（本次没有产出结论）" }}</p>

      <template v-if="findings.length">
        <h2 class="section-title">确定性审计发现</h2>
        <div class="findings reveal">
          <div
            v-for="(issue, i) in findings"
            :key="issue.id ?? i"
            class="finding"
            :class="{ warnrow: issue.severity !== 'error' }"
          >
            <span class="badge">{{ issue.severity === "error" ? "错误" : "提醒" }}</span>
            <b class="mono">{{ issue.rule_code }}</b>
            <span class="mono ref-cell">{{ issue.target_ref }}</span>
            <span class="msg">{{ issue.message }}</span>
            <span v-if="issue.suggested_action" class="muted small suggest">
              建议：{{ issue.suggested_action }}
            </span>
            <span v-if="issue.source_path" class="muted small loc mono">
              {{ issue.source_path }}<template v-if="issue.source_line">:{{ issue.source_line }}</template>
            </span>
          </div>
        </div>
      </template>

      <h2 class="section-title">取证轨迹</h2>
      <ol class="trace">
        <li v-for="(step, i) in result.steps" :key="i" :class="{ bad: step.is_error }">
          <b class="mono">{{ step.action }}</b>
          <span class="muted args mono">{{ JSON.stringify(step.action_input) }}</span>
          <p v-if="step.thought" class="muted small thought">{{ step.thought }}</p>
          <p class="muted small obs">
            观测 {{ step.observation_chars }} 字<span v-if="step.is_truncated">（已截断，模型可回读）</span>
          </p>
        </li>
      </ol>
      <template v-if="result.mcp_traces?.length">
        <h2 class="section-title">MCP 调用轨迹</h2>
        <div class="mcp-traces">
          <div v-for="(trace, i) in result.mcp_traces" :key="i" class="mcp-trace">
            <b class="mono">{{ trace.tool_name ?? "MCP tool" }}</b>
            <span>{{ trace.status ?? "completed" }}</span>
            <span v-if="trace.duration_ms != null" class="muted">{{ trace.duration_ms }} ms</span>
            <span v-if="trace.risk_flags?.length" class="warn">{{ trace.risk_flags.join("、") }}</span>
          </div>
        </div>
      </template>
      <div v-if="result.run_budget" class="budget-grid">
        <span>模型调用 {{ result.run_budget.total_model_calls_reserved ?? 0 }} / {{ result.run_budget.total_model_call_limit ?? "不限" }}</span>
        <span>工具上限 {{ result.run_budget.tool_call_limit ?? "不限" }}</span>
        <span>已结算 ${{ (result.run_budget.settled_cost_usd ?? 0).toFixed(4) }} / {{ result.run_budget.max_cost_usd ?? "不限" }}</span>
      </div>
      <p class="muted small">
        本次可用工具：{{ result.skills.join("、") }}。要处置修复提案，请到
        <RouterLink to="/audit">「校勘修复」</RouterLink> 逐条验证、应用或回滚。
      </p>
    </div>
  </section>
</template>

<style scoped>
.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field.narrow {
  max-width: 9rem;
}

.row {
  display: flex;
  gap: 1.2rem;
  align-items: flex-end;
  flex-wrap: wrap;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

textarea,
input[type="number"],
input[type="text"],
input:not([type]),
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
  resize: vertical;
}
.advanced-toggle {
  min-height: 44px;
  border: 1px solid var(--ow-line);
  color: var(--ow-gold-bright);
  background: transparent;
  text-align: left;
  padding: 0.55rem 0.7rem;
  cursor: pointer;
}
.advanced-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.75rem;
  padding: 0.8rem;
  border: 1px solid var(--ow-line);
  background: color-mix(in srgb, var(--ow-panel-2) 70%, transparent);
}
.span-two { grid-column: 1 / -1; }
.mcp-traces, .budget-grid { display: grid; gap: 0.45rem; }
.mcp-trace, .budget-grid span {
  display: flex;
  gap: 0.7rem;
  flex-wrap: wrap;
  padding: 0.5rem 0.65rem;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  font-size: 0.8rem;
}
@media (max-width: 720px) {
  .advanced-grid { grid-template-columns: 1fr; }
  .span-two { grid-column: auto; }
}

textarea:focus,
input:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.check {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  font-size: 0.85rem;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
  padding: 0.55rem 1rem;
  cursor: pointer;
  align-self: flex-start;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

button.ghost {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  font: inherit;
  font-size: 0.85rem;
  padding: 0.5rem 0.9rem;
  cursor: pointer;
}

.gate,
.done {
  margin-top: 0.9rem;
  padding: 0.95rem 1.15rem;
}

.gate {
  border-color: var(--ow-gold-soft);
}

.gate-title,
.section-title {
  font-size: 0.95rem;
  color: var(--ow-gold-bright);
  margin: 0.8rem 0 0.4rem;
}

.gate-title {
  margin-top: 0;
}

.gate-actions {
  display: flex;
  gap: 0.6rem;
  margin-top: 0.7rem;
}

.pending {
  display: flex;
  gap: 0.6rem;
  align-items: baseline;
  flex-wrap: wrap;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  padding: 0.5rem 0.75rem;
  margin-bottom: 0.6rem;
  font-size: 0.85rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.chip {
  border: 1px solid var(--ow-line);
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

.answer {
  font-size: 0.9rem;
  line-height: 1.65;
  margin: 0;
}

.findings {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.finding {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 0.3rem 0.7rem;
  align-items: baseline;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  padding: 0.5rem 0.75rem;
  font-size: 0.85rem;
}

.finding .msg,
.finding .suggest,
.finding .loc {
  grid-column: 1 / -1;
}

.ref-cell {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.badge {
  border: 1px solid rgba(224, 133, 133, 0.45);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: #e89a9a;
  font-size: 0.72rem;
  padding: 0.08rem 0.5rem;
}

.finding.warnrow .badge {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.trace {
  margin: 0;
  padding-left: 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  font-size: 0.85rem;
}

.trace li.bad b {
  color: #e89a9a;
}

.trace p {
  margin: 0.15rem 0 0;
}

.args {
  font-size: 0.76rem;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.78rem;
}

.small {
  font-size: 0.78rem;
}

@media (max-width: 600px) {
  .finding { grid-template-columns: 1fr; align-items: start; }
  .finding > * { min-width: 0; overflow-wrap: anywhere; }
  .gate-actions { align-items: stretch; flex-direction: column; }
  .gate-actions button { width: 100%; min-height: 44px; }
}
</style>
