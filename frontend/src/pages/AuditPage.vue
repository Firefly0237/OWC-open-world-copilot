<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  humanizeError,
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentOperator,
  currentProject,
  llmConfig,
  setCurrentOperator,
} from "../api";
import PageHead from "../components/PageHead.vue";
import { notifyError, pushToast } from "../toast";

interface Issue {
  id: string;
  rule_code: string;
  severity: string;
  category: string;
  target_ref: string;
  message: string;
  status?: string;
}

interface Candidate {
  patch_id: string;
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

// per-issue suggestion state, keyed by issue id
const suggestState = ref<Record<string, { busy: boolean; candidates: Candidate[]; note: string }>>({});
// per-patch apply/rollback outcome
const patchState = ref<Record<string, { applied?: boolean; rolledBack?: boolean; note: string }>>({});

const llmReady = ref(llmConfig().ready);

const filtered = computed(() =>
  filter.value === "all" ? issues.value : issues.value.filter((i) => i.severity === filter.value),
);

onMounted(() => {
  window.addEventListener("ow-llm-changed", () => (llmReady.value = llmConfig().ready));
});

async function loadIssues(): Promise<void> {
  const body = await apiGet<{ issues: Issue[] }>(`/projects/${currentProject()}/issues`);
  issues.value = body.issues;
}

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
const contraRan = ref(false);
const contra = ref<{ contradictions: Contra[]; review_suggested: Contra[]; llm_used: boolean }>({
  contradictions: [],
  review_suggested: [],
  llm_used: false,
});
async function scanContradictions(): Promise<void> {
  contraScanning.value = true;
  try {
    const body = await apiPost<typeof contra.value & { cost_budget?: Record<string, unknown> }>(
      `/projects/${currentProject()}/contradictions:scan`,
      { use_llm: true, llm_mode: "real" },
    );
    contra.value = body;
    contraRan.value = true;
    if (body.cost_budget) addSessionCost(costOf(body.cost_budget));
  } catch (e) {
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
      `/projects/${currentProject()}/audits`,
      { persist: true },
    );
    totals.value = body.totals;
    await loadIssues();
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
      ? `${body.used_llm ? "模型+确定性" : "确定性修复器"}给出 ${body.candidates.length} 个候选${
          body.rejected_count ? `（影子校验淘汰 ${body.rejected_count} 个）` : ""
        }`
      : `没有可用候选${body.rejected_count ? `（${body.rejected_count} 个被影子校验淘汰）` : "——可能需要人工处理"}`;
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
    pushToast("先填署名再应用修复。", "error");
    return;
  }
  setCurrentOperator(operator.value.trim());
  try {
    const body = await apiPost<{ applied: boolean; reason: string; resolved_errors: number; introduced_errors: number }>(
      `/projects/${currentProject()}/patches/${encodeURIComponent(candidate.patch_id)}:apply`,
      { operator: operator.value.trim() },
    );
    patchState.value = {
      ...patchState.value,
      [candidate.patch_id]: {
        applied: body.applied,
        note: body.applied
          ? `已应用：解决 ${body.resolved_errors} 个错误${body.introduced_errors ? `，引入 ${body.introduced_errors} 个` : "，未引入新错误"}`
          : `未应用：${body.reason}`,
      },
    };
    if (body.applied) await runAudit();
  } catch (e) {
    patchState.value = { ...patchState.value, [candidate.patch_id]: { note: humanizeError(e) } };
  }
}

async function rollback(candidate: Candidate): Promise<void> {
  if (!operator.value.trim()) {
    pushToast("先填署名再回滚。", "error");
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
</script>

<template>
  <section>
    <PageHead overline="AUDIT" title="校勘修复" purpose="检查一致性问题，每条给出可回滚的修复。" />

    <div class="bar">
      <input v-model="operator" class="op" placeholder="署名（应用/回滚时必填）" />
      <button class="primary" :disabled="running" @click="runAudit">
        {{ running ? "审计中…" : ran ? "重新审计" : "运行审计" }}
      </button>
      <span v-if="ran" class="chips">
        <span class="chip red">错误 <b>{{ totals.error ?? 0 }}</b></span>
        <span class="chip amber">警告 <b>{{ totals.warn ?? totals.warning ?? 0 }}</b></span>
        <span class="chip">提示 <b>{{ totals.info ?? 0 }}</b></span>
      </span>
    </div>

    <div class="pane contra">
      <div class="contra-head">
        <div class="section"><span class="t">设定矛盾检测</span></div>
        <button class="ghost" :disabled="contraScanning" @click="scanContradictions">
          {{ contraScanning ? "扫描中…" : "扫描设定矛盾" }}
        </button>
      </div>
      <p class="muted small">
        结构审计看不出"语义矛盾"——同一对势力这里写盟友、那里写死敌。先语义召回可疑对，模型判官确认真矛盾，人工消解。
      </p>
      <div v-if="contraRan">
        <p v-if="!contra.contradictions.length && !contra.review_suggested.length" class="ok-text">
          没有发现互相矛盾的设定——干净。
        </p>
        <div v-for="(c, k) in contra.contradictions" :key="'c' + k" class="contra-card hit">
          <div class="contra-tag">矛盾 · {{ c.subjects.join(" ↔ ") }}</div>
          <p class="contra-point">{{ c.point }}</p>
          <p v-for="(s, i) in c.statements" :key="i" class="contra-stmt mono">{{ s }}</p>
        </div>
        <div v-for="(c, k) in contra.review_suggested" :key="'r' + k" class="contra-card review">
          <div class="contra-tag review">待人工确认</div>
          <p class="contra-point">{{ c.point }}</p>
          <p v-for="(s, i) in c.statements" :key="i" class="contra-stmt mono">{{ s }}</p>
        </div>
      </div>
    </div>

    <div v-if="ran" class="filters">
      <button v-for="f in ['all', 'error', 'warn', 'info']" :key="f" class="pill" :class="{ on: filter === f }" @click="filter = f">
        {{ f === "all" ? "全部" : SEV_LABEL[f] }}
      </button>
    </div>

    <p v-if="ran && !filtered.length" class="ok-text">这一档没有问题——干净。</p>

    <TransitionGroup name="list" tag="div" class="issues">
      <div v-for="issue in filtered" :key="issue.id" class="pane issue" :class="issue.severity">
        <div class="head">
          <span class="sev" :class="issue.severity">{{ SEV_LABEL[issue.severity] ?? issue.severity }}</span>
          <span class="rule mono">{{ issue.rule_code }}</span>
          <span class="ref mono">{{ issue.target_ref }}</span>
        </div>
        <p class="msg">{{ issue.message }}</p>
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
  border-radius: 0.5rem;
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
  border-radius: 0.5rem;
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
  border-radius: 0.5rem;
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
  border-radius: 999px;
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
  border-radius: 999px;
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

.sev {
  font-size: 0.74rem;
  border-radius: 999px;
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

.issue-actions {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
}

.tiny {
  font-size: 0.74rem;
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
  border-radius: 999px;
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
</style>
