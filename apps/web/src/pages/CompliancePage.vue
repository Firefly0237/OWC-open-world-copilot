<script setup lang="ts">
import { computed, onMounted, ref, watch } from "vue";
import {
  apiGet,
  apiPost,
  currentOperator,
  currentProject,
  humanizeError,
  llmConfig,
  llmParams,
  setCurrentOperator,
} from "../api";
import AsyncState from "../components/AsyncState.vue";
import PageHead from "../components/PageHead.vue";
import { notifyOk, pushToast } from "../toast";

interface CaseEvent {
  at: string;
  operator: string;
  from_status: string;
  to_status: string;
  note: string;
}
interface Case {
  id: string;
  object_ref: string;
  category: string;
  evidence: string;
  status: string;
  assignee: string;
  history: CaseEvent[];
}
interface Report {
  total: number;
  by_status: Record<string, number>;
  open_unresolved: number;
  signed_off: number;
  cases: Case[];
}
type BusyOperation =
  | { kind: "scan" }
  | { kind: "transition"; caseId: string; to: string }
  | { kind: "rescan"; caseId: string };

const report = ref<Report | null>(null);
const operator = ref(currentOperator());
const loading = ref(true);
const refreshing = ref(false);
const loadError = ref("");
const mutationError = ref("");
const busy = ref<BusyOperation | null>(null);
const expanded = ref<string | null>(null);
const caseTotal = ref(0);
const nextCaseCursor = ref<string | null>(null);
const loadingMoreCases = ref(false);
const statusFilter = ref("");
const assigneeFilter = ref("");
const rulePackText = ref(localStorage.getItem("owcopilot_compliance_rule_pack") ?? "");
const controlsBusy = computed(
  () => loading.value || refreshing.value || loadingMoreCases.value || busy.value !== null,
);
const scanBusy = computed(() => busy.value?.kind === "scan");
let loadSequence = 0;

const STATUS_LABEL: Record<string, string> = {
  flagged: "待处理",
  assigned: "已指派",
  fixing: "整改中",
  rescan_passed: "复扫通过·待签核",
  signed_off: "已签核",
  dismissed: "已关闭",
};

// status -> [ {to, label}, ... ] manual actions (rescan is a separate endpoint)
const ACTIONS: Record<string, { to: string; label: string; needsAssignee?: boolean }[]> = {
  flagged: [
    { to: "assigned", label: "指派给我", needsAssignee: true },
    { to: "dismissed", label: "驳回" },
  ],
  assigned: [
    { to: "fixing", label: "开始整改" },
    { to: "dismissed", label: "驳回" },
  ],
  rescan_passed: [
    { to: "signed_off", label: "签核" },
    { to: "fixing", label: "打回" },
    { to: "dismissed", label: "驳回" },
  ],
};

async function load(append = false): Promise<void> {
  if (append && !nextCaseCursor.value) return;
  const sequence = ++loadSequence;
  const isInitial = report.value === null;
  if (isInitial) loading.value = true;
  else if (append) loadingMoreCases.value = true;
  else refreshing.value = true;
  loadError.value = "";
  try {
    const query = new URLSearchParams({ limit: "50" });
    if (statusFilter.value) query.set("status", statusFilter.value);
    if (assigneeFilter.value.trim()) query.set("assignee", assigneeFilter.value.trim());
    if (append && nextCaseCursor.value) query.set("cursor", nextCaseCursor.value);
    const body = await apiGet<{
      report: Report;
      total: number;
      next_cursor: string | null;
    }>(`/projects/${currentProject()}/compliance/report?${query.toString()}`);
    if (sequence !== loadSequence) return;
    if (append && report.value) {
      report.value = { ...body.report, cases: [...report.value.cases, ...body.report.cases] };
    } else {
      report.value = body.report;
    }
    caseTotal.value = body.total;
    nextCaseCursor.value = body.next_cursor;
  } catch (e) {
    if (sequence !== loadSequence) return;
    loadError.value = humanizeError(e);
    if (append) await load(false);
  } finally {
    if (sequence === loadSequence) {
      loading.value = false;
      refreshing.value = false;
      loadingMoreCases.value = false;
    }
  }
}
onMounted(load);
watch([statusFilter, assigneeFilter], (_value, _oldValue, onCleanup) => {
  const timer = window.setTimeout(() => void load(false), 250);
  onCleanup(() => window.clearTimeout(timer));
});

function saveOperator(): void {
  setCurrentOperator(operator.value.trim());
}

function showMutationError(error: unknown): void {
  const message = humanizeError(error);
  mutationError.value = message;
  pushToast(message, "error", 6000);
}

function transitionBusy(c: Case, to: string): boolean {
  const operation = busy.value;
  return operation?.kind === "transition" && operation.caseId === c.id && operation.to === to;
}

function rescanBusy(c: Case): boolean {
  const operation = busy.value;
  return operation?.kind === "rescan" && operation.caseId === c.id;
}

function complianceModelPolicy(): Record<string, unknown> {
  return llmConfig().ready ? llmParams() : { llm_mode: "offline" };
}

function scanPolicy(): Record<string, unknown> {
  const source = rulePackText.value.trim();
  if (!source) return complianceModelPolicy();
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error("自定义规则包不是合法 JSON，请检查括号、引号和逗号。");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("自定义规则包必须是 JSON 对象。");
  }
  localStorage.setItem("owcopilot_compliance_rule_pack", JSON.stringify(parsed, null, 2));
  return { rule_pack: parsed, ...complianceModelPolicy() };
}

async function scan(): Promise<void> {
  if (controlsBusy.value) return;
  busy.value = { kind: "scan" };
  mutationError.value = "";
  try {
    const res = await apiPost<{ hits: number; report: Report }>(
      `/projects/${currentProject()}/compliance:scan?include_details=false`,
      scanPolicy(),
    );
    await load(false);
    loadError.value = "";
    notifyOk(`清查完成，命中 ${res.hits} 处。`);
  } catch (e) {
    showMutationError(e);
  } finally {
    busy.value = null;
  }
}

async function act(c: Case, to: string, needsAssignee?: boolean): Promise<void> {
  if (!operator.value.trim()) {
    showMutationError(new Error("请先填写署名"));
    return;
  }
  if (controlsBusy.value) return;
  busy.value = { kind: "transition", caseId: c.id, to };
  mutationError.value = "";
  try {
    await apiPost(`/projects/${currentProject()}/compliance/cases/${c.id}:transition`, {
      to,
      operator: operator.value.trim(),
      assignee: needsAssignee ? operator.value.trim() : null,
    });
    await load();
  } catch (e) {
    showMutationError(e);
  } finally {
    busy.value = null;
  }
}

async function rescan(c: Case): Promise<void> {
  if (!operator.value.trim()) {
    showMutationError(new Error("请先填写署名"));
    return;
  }
  if (controlsBusy.value) return;
  busy.value = { kind: "rescan", caseId: c.id };
  mutationError.value = "";
  try {
    const res = await apiPost<{ still_flagged: boolean }>(
      `/projects/${currentProject()}/compliance/cases/${c.id}:rescan`,
      { operator: operator.value.trim(), ...scanPolicy() },
    );
    notifyOk(res.still_flagged ? "复扫仍命中，已打回整改。" : "复扫通过，待签核。");
    await load();
  } catch (e) {
    showMutationError(e);
  } finally {
    busy.value = null;
  }
}

const sortedCases = computed(() => (report.value ? report.value.cases : []));
</script>

<template>
  <section :aria-busy="controlsBusy || undefined">
    <PageHead
      overline="COMPLIANCE"
      title="版号合规整改"
      purpose="把敏感词清查升级为整改闭环：标记→指派→整改→复扫→签核，全程留痕。"
    />
    <div class="bar">
      <input
        v-model="operator"
        aria-label="操作人署名"
        placeholder="署名（必填）"
        :disabled="controlsBusy"
        @blur="saveOperator"
      />
      <button
        type="button"
        class="primary"
        :disabled="controlsBusy"
        :aria-busy="scanBusy || undefined"
        @click="scan"
      >
        {{ scanBusy ? "清查中…" : "运行清查" }}
      </button>
      <button type="button" class="ghost" :disabled="controlsBusy" @click="load()">
        {{ loading || refreshing ? "刷新中…" : "刷新" }}
      </button>
      <select v-model="statusFilter" :disabled="controlsBusy" aria-label="按案件状态筛选">
        <option value="">全部状态</option>
        <option v-for="(label, value) in STATUS_LABEL" :key="value" :value="value">{{ label }}</option>
      </select>
      <input
        v-model="assigneeFilter"
        aria-label="按负责人筛选"
        placeholder="筛选负责人"
        :disabled="controlsBusy"
      />
    </div>

    <details class="rule-pack">
      <summary>自定义规则包与语义复扫 <span class="muted">高级</span></summary>
      <p class="muted small">
        留空使用内置示例包；填写后，初扫和单案复扫会使用同一版本化规则包。未接入模型时运行 $0
        确定性词表清查，已接入时同时启用语义判定。
      </p>
      <textarea
        v-model="rulePackText"
        rows="7"
        :disabled="controlsBusy"
        aria-label="自定义合规规则包 JSON"
        placeholder='{"id":"cn-release-v1","name":"发行规则包","version_label":"v1","categories":["涉赌"],"terms":["赌博","私服"],"semantic_threshold":0.6}'
      ></textarea>
    </details>

    <div v-if="mutationError" class="mutation-error" role="alert">
      <div>
        <strong>操作未完成</strong>
        <span>{{ mutationError }}</span>
      </div>
      <button type="button" class="ghost" @click="mutationError = ''">收起</button>
    </div>

    <AsyncState
      v-if="loading && !report"
      state="loading"
      title="正在读取合规整改台账"
      hint="案件状态、负责人和流转记录正在汇总。"
      compact
    />
    <AsyncState
      v-else-if="loadError && !report"
      state="error"
      title="合规整改台账读取失败"
      :hint="loadError"
      action-label="重新读取"
      compact
      @action="load"
    />
    <template v-else-if="report">
      <AsyncState
        v-if="loadError"
        state="error"
        title="台账刷新失败"
        :hint="loadError"
        action-label="重新刷新"
        compact
        @action="load"
      />

      <div class="summary">
        <span class="s-chip">共 {{ report.total }} 案</span>
        <span class="s-chip">已显示 {{ report.cases.length }} / {{ caseTotal }}</span>
        <span class="s-chip warn">未结 {{ report.open_unresolved }}</span>
        <span class="s-chip ok">已签核 {{ report.signed_off }}</span>
      </div>

      <AsyncState
        v-if="!report.cases.length"
        state="empty"
        title="尚无整改案件"
        hint="运行一次清查；命中的内容会进入可指派、整改、复扫和签核的闭环。"
        compact
      />

      <article v-for="c in sortedCases" :key="c.id" class="pane case reveal">
        <header class="c-header">
          <button
            type="button"
            class="c-head"
            :aria-expanded="expanded === c.id"
            :aria-controls="`case-history-${c.id}`"
            @click="expanded = expanded === c.id ? null : c.id"
          >
            <span class="c-status" :class="c.status">
              {{ STATUS_LABEL[c.status] ?? c.status }}
            </span>
            <span class="c-cat">{{ c.category }}</span>
            <span class="c-ref mono">{{ c.object_ref }}</span>
            <span class="c-head-tail">
              <span v-if="c.assignee" class="c-assignee">@{{ c.assignee }}</span>
              <span class="c-disclosure" aria-hidden="true">
                {{ expanded === c.id ? "−" : "+" }}
              </span>
            </span>
          </button>
        </header>
        <p class="c-evidence">{{ c.evidence }}</p>
        <div class="c-actions">
          <button
            v-for="a in ACTIONS[c.status] ?? []"
            :key="a.to"
            type="button"
            class="ghost"
            :disabled="controlsBusy"
            :aria-busy="transitionBusy(c, a.to) || undefined"
            @click="act(c, a.to, a.needsAssignee)"
          >
            {{ transitionBusy(c, a.to) ? `${a.label}中…` : a.label }}
          </button>
          <button
            v-if="c.status === 'fixing'"
            type="button"
            class="ghost"
            :disabled="controlsBusy"
            :aria-busy="rescanBusy(c) || undefined"
            @click="rescan(c)"
          >
            {{ rescanBusy(c) ? "复扫中…" : "复扫" }}
          </button>
        </div>
        <div v-show="expanded === c.id" :id="`case-history-${c.id}`" class="c-history">
          <div v-for="(e, i) in c.history" :key="i" class="h-row">
            <span class="mono">{{ e.from_status }} → {{ e.to_status }}</span>
            <span class="muted">{{ e.operator }}</span>
            <span v-if="e.note" class="h-note">{{ e.note }}</span>
          </div>
          <p v-if="!c.history.length" class="muted h-empty">暂无流转记录。</p>
        </div>
      </article>
      <div v-if="nextCaseCursor" class="pagination-bar" aria-live="polite">
        <button class="ghost" :disabled="loadingMoreCases" @click="load(true)">
          {{ loadingMoreCases ? "加载中…" : "加载下一页" }}
        </button>
      </div>
    </template>
    <AsyncState
      v-else
      state="empty"
      title="还没有合规整改台账"
      hint="运行清查后，整改案件会显示在这里。"
      compact
    />
  </section>
</template>

<style scoped>
.bar {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 1rem;
}
.bar input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
  width: 12rem;
}
.bar .primary {
  background: var(--ow-gold, #d8b46a);
  color: #1a1406;
  border: none;
  border-radius: var(--ow-control-radius);
  padding: 0.45rem 1rem;
  cursor: pointer;
}
.bar select {
  min-height: 40px;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  background: var(--ow-panel-2);
  padding: 0.45rem 0.7rem;
}
.pagination-bar {
  display: flex;
  justify-content: center;
  margin: 1rem 0;
}
.bar input:disabled,
.bar button:disabled,
.c-actions button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
.rule-pack {
  margin: -0.35rem 0 1rem;
  padding: 0.65rem 0.8rem;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
}
.rule-pack summary { cursor: pointer; color: var(--ow-ink); }
.rule-pack textarea {
  width: 100%;
  box-sizing: border-box;
  resize: vertical;
  padding: 0.6rem 0.7rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-night-deep);
  color: var(--ow-cyan);
  font: 0.78rem/1.5 ui-monospace, Consolas, monospace;
}
.mutation-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 1rem;
  margin-bottom: 1rem;
  padding: 0.7rem 0.8rem;
  border: 1px solid rgba(224, 112, 90, 0.42);
  background: rgba(224, 112, 90, 0.09);
  color: #efaa9a;
  line-height: 1.5;
}
.mutation-error > div {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
}
.mutation-error strong {
  color: var(--ow-ink);
  font-size: 0.82rem;
}
.summary {
  display: flex;
  gap: 0.5rem;
  margin: 1rem 0;
}
.s-chip {
  font-size: 0.78rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  padding: 0.2rem 0.55rem;
  color: var(--ow-ink-dim);
}
.s-chip.warn {
  color: var(--ow-flag, #e0653a);
}
.s-chip.ok {
  color: #6fcf97;
}
.case {
  margin-bottom: 0.8rem;
  padding: 0.9rem 1rem;
}
.c-header {
  margin: 0;
}
.c-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  width: 100%;
  min-height: 44px;
  padding: 0.15rem 0;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  font: inherit;
  text-align: left;
}
.c-head:focus-visible {
  outline: 2px solid var(--ow-cyan);
  outline-offset: 4px;
}
.c-status {
  font-size: 0.72rem;
  border-radius: 0.3rem;
  padding: 0.1rem 0.45rem;
  border: 1px solid var(--ow-line);
  white-space: nowrap;
}
.c-status.signed_off {
  color: #6fcf97;
  border-color: #6fcf97;
}
.c-status.fixing,
.c-status.flagged {
  color: var(--ow-flag, #e0653a);
}
.c-cat {
  font-size: 0.8rem;
  color: var(--ow-ink-dim);
}
.c-ref {
  font-size: 0.76rem;
  color: var(--ow-ink-dim);
  overflow-wrap: anywhere;
}
.c-head-tail {
  display: inline-flex;
  align-items: center;
  gap: 0.55rem;
  margin-left: auto;
}
.c-assignee {
  font-size: 0.74rem;
  color: var(--ow-gold, #d8b46a);
}
.c-disclosure {
  display: inline-grid;
  width: 1.6rem;
  height: 1.6rem;
  place-items: center;
  border: 1px solid var(--ow-line);
  color: var(--ow-gold, #d8b46a);
  font-family: var(--ow-num);
  line-height: 1;
}
.c-evidence {
  font-size: 0.85rem;
  margin: 0.5rem 0;
}
.c-actions {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.c-history {
  margin-top: 0.6rem;
  border-top: 1px solid var(--ow-line);
  padding-top: 0.5rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.h-row {
  display: flex;
  gap: 0.6rem;
  font-size: 0.74rem;
}
.h-note {
  color: var(--ow-ink-dim);
}
.h-empty {
  margin: 0;
  font-size: 0.76rem;
}

@media (max-width: 600px) {
  .bar { align-items: stretch; flex-direction: column; }
  .bar input,
  .bar button { width: 100%; min-height: 44px; }
  .summary { flex-wrap: wrap; }
  .mutation-error { align-items: stretch; flex-direction: column; }
  .mutation-error button,
  .c-actions button { width: 100%; min-height: 44px; }
  .c-head { align-items: flex-start; flex-wrap: wrap; }
  .c-head-tail { min-height: 44px; }
  .h-row { align-items: flex-start; flex-direction: column; gap: 0.15rem; }
}
</style>
