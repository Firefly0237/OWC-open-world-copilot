<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiGet, apiPost, currentOperator, currentProject, setCurrentOperator } from "../api";
import { notifyError, notifyOk } from "../toast";
import PageHead from "../components/PageHead.vue";

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

const report = ref<Report | null>(null);
const operator = ref(currentOperator());
const busy = ref(false);
const expanded = ref<string | null>(null);

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

async function load(): Promise<void> {
  try {
    report.value = (
      await apiGet<{ report: Report }>(`/projects/${currentProject()}/compliance/report`)
    ).report;
  } catch (e) {
    notifyError(e);
  }
}
onMounted(load);

function saveOperator(): void {
  setCurrentOperator(operator.value.trim());
}

async function scan(): Promise<void> {
  busy.value = true;
  try {
    const res = await apiPost<{ hits: number; report: Report }>(
      `/projects/${currentProject()}/compliance:scan`,
      {},
    );
    report.value = res.report;
    notifyOk(`清查完成，命中 ${res.hits} 处。`);
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

async function act(c: Case, to: string, needsAssignee?: boolean): Promise<void> {
  if (!operator.value.trim()) {
    notifyError(new Error("请先填写署名"));
    return;
  }
  busy.value = true;
  try {
    await apiPost(`/projects/${currentProject()}/compliance/cases/${c.id}:transition`, {
      to,
      operator: operator.value.trim(),
      assignee: needsAssignee ? operator.value.trim() : null,
    });
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

async function rescan(c: Case): Promise<void> {
  if (!operator.value.trim()) {
    notifyError(new Error("请先填写署名"));
    return;
  }
  busy.value = true;
  try {
    const res = await apiPost<{ still_flagged: boolean }>(
      `/projects/${currentProject()}/compliance/cases/${c.id}:rescan`,
      { operator: operator.value.trim() },
    );
    notifyOk(res.still_flagged ? "复扫仍命中，已打回整改。" : "复扫通过，待签核。");
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

const sortedCases = computed(() =>
  report.value ? report.value.cases : [],
);
</script>

<template>
  <section>
    <PageHead
      overline="COMPLIANCE"
      title="版号合规整改"
      purpose="把敏感词清查升级为整改闭环：标记→指派→整改→复扫→签核，全程留痕。"
    />
    <div class="bar">
      <input v-model="operator" placeholder="署名（必填）" @blur="saveOperator" />
      <button class="primary" :disabled="busy" @click="scan">运行清查</button>
      <button class="ghost" :disabled="busy" @click="load">刷新</button>
    </div>

    <div v-if="report" class="summary">
      <span class="s-chip">共 {{ report.total }} 案</span>
      <span class="s-chip warn">未结 {{ report.open_unresolved }}</span>
      <span class="s-chip ok">已签核 {{ report.signed_off }}</span>
    </div>

    <p v-if="report && !report.cases.length" class="muted empty">尚无整改案件。点「运行清查」开始。</p>

    <article v-for="c in sortedCases" :key="c.id" class="pane case reveal">
      <header class="c-head" @click="expanded = expanded === c.id ? null : c.id">
        <span class="c-status" :class="c.status">{{ STATUS_LABEL[c.status] ?? c.status }}</span>
        <span class="c-cat">{{ c.category }}</span>
        <span class="c-ref mono">{{ c.object_ref }}</span>
        <span v-if="c.assignee" class="c-assignee">@{{ c.assignee }}</span>
      </header>
      <p class="c-evidence">{{ c.evidence }}</p>
      <div class="c-actions">
        <button
          v-for="a in ACTIONS[c.status] ?? []"
          :key="a.to"
          class="ghost"
          :disabled="busy"
          @click="act(c, a.to, a.needsAssignee)"
        >
          {{ a.label }}
        </button>
        <button v-if="c.status === 'fixing'" class="ghost" :disabled="busy" @click="rescan(c)">
          复扫
        </button>
      </div>
      <div v-if="expanded === c.id && c.history.length" class="c-history">
        <div v-for="(e, i) in c.history" :key="i" class="h-row">
          <span class="mono">{{ e.from_status }} → {{ e.to_status }}</span>
          <span class="muted">{{ e.operator }}</span>
          <span v-if="e.note" class="h-note">{{ e.note }}</span>
        </div>
      </div>
    </article>
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
.summary {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 1rem;
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
.empty {
  padding: 2rem 0;
}
.case {
  margin-bottom: 0.8rem;
  padding: 0.9rem 1rem;
}
.c-head {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  cursor: pointer;
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
}
.c-assignee {
  margin-left: auto;
  font-size: 0.74rem;
  color: var(--ow-gold, #d8b46a);
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
</style>
