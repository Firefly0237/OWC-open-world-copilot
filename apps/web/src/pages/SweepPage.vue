<script setup lang="ts">
import { computed, onMounted, ref, toRef, toRefs } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import PageHead from "../components/PageHead.vue";
import {
  apiPost,
  currentOperator,
  currentProject,
  llmConfig,
  llmParams,
  setCurrentOperator,
} from "../api";
import { getJobChannel, resumeJob, startJob } from "../jobs";
import { notifyError, notifyOk, pushToast } from "../toast";
import { example } from "../examples";

const phTheme = example("sweepTheme");

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "scan", label: "词面扫描" },
  { key: "judge", label: "语义判定" },
  { key: "done", label: "清单" },
];

const FLAVORS: Record<string, string> = {
  accepted: "受理清查请求…",
  scan: "逐字扫过全世界…",
  judge: "按语义逐条判定…",
  done: "清单已就绪。",
};

interface Finding {
  ref: string;
  name: string;
  object_kind: string;
  layer: string;
  evidence: string;
  verdict: string;
  // sentence-level location within the object; stable dismissal key
  locator: string;
  finding_id: string;
}

interface SweepResult {
  scanned_total: number;
  llm_used: boolean;
  semantic_used: boolean;
  semantic_flagged: number;
  judged_count: number;
  judge_skipped: number;
  hits: Finding[];
  review_suggested: Finding[];
  dismissed_count: number;
  markdown: string;
}

const theme = ref("");
const terms = ref("");
const useJudge = ref(llmConfig().ready);
const maxJudge = ref(400);
const semanticThreshold = ref(0.5);
const operator = ref(currentOperator());
// findings the reviewer excluded this session (逐一排除); counted on top of the server's
// dismissed_count so the honesty line stays accurate before the next re-run.
const dismissedNow = ref(0);
const dismissing = ref<Record<string, boolean>>({});

const job = getJobChannel<SweepResult>("theme_sweep", STAGES);
const { running, stageIndex, elapsed, result } = toRefs(job);
const judgeNote = toRef(job, "hint");
const lastCost = toRef(job, "cost");
const FINDING_PAGE_SIZE = 50;
const visibleFindingCount = ref(FINDING_PAGE_SIZE);
const allFindings = computed(() => [
  ...(result.value?.hits ?? []),
  ...(result.value?.review_suggested ?? []),
]);
const visibleFindings = computed(() => allFindings.value.slice(0, visibleFindingCount.value));

function normalizedMaxJudge(): number | null {
  const value = Number(maxJudge.value);
  return Number.isInteger(value) && value >= 1 && value <= 2000 ? value : null;
}

function normalizedSemanticThreshold(): number | null {
  const value = Number(semanticThreshold.value);
  return Number.isFinite(value) && value >= 0 && value <= 1 ? value : null;
}

const maxJudgeValid = computed(() => normalizedMaxJudge() !== null);
const semanticThresholdValid = computed(() => normalizedSemanticThreshold() !== null);

function onSweepEvent(ch: { stageIndex: number; hint: string }, event: { type: string; data: Record<string, unknown> }): void {
  if (event.type === "judge") {
    ch.stageIndex = 2;
    ch.hint = `已判定 ${event.data.done}/${event.data.total}`;
  }
}

const parseSweepResult = (raw: Record<string, unknown>): SweepResult =>
  raw as unknown as SweepResult;

onMounted(() => {
  void resumeJob<SweepResult>("theme_sweep", {
    stages: STAGES,
    onEvent: onSweepEvent,
    parseResult: parseSweepResult,
  });
});

async function run(): Promise<void> {
  const judgeLimit = normalizedMaxJudge();
  const threshold = normalizedSemanticThreshold();
  if (!theme.value.trim() || running.value || judgeLimit === null || threshold === null) return;
  dismissedNow.value = 0;
  visibleFindingCount.value = FINDING_PAGE_SIZE;
  await startJob<SweepResult>("theme_sweep", {
    kind: "theme_sweep",
    stages: STAGES,
    params: {
      theme: theme.value.trim(),
      extra_terms: terms.value
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
      use_llm: useJudge.value,
      ...(useJudge.value ? llmParams() : {}),
      max_judge: judgeLimit,
      semantic_threshold: threshold,
    },
    onEvent: onSweepEvent,
    parseResult: parseSweepResult,
  });
}

function downloadWorkOrder(): void {
  if (!result.value) return;
  const blob = new Blob([result.value.markdown], { type: "text/markdown;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `sweep-${theme.value.trim().slice(0, 20)}.md`;
  link.click();
  URL.revokeObjectURL(link.href);
}

// 逐一排除: mark ONE finding a false positive. Signed (operator) + persisted server-side, so the
// next sweep of the SAME theme suppresses it. We optimistically drop it from the list here.
async function dismiss(finding: Finding): Promise<void> {
  if (!operator.value.trim()) {
    pushToast("先填上你的名字（用于记录谁排除了这条）。", "error");
    return;
  }
  if (dismissing.value[finding.finding_id]) return;
  setCurrentOperator(operator.value.trim());
  dismissing.value = { ...dismissing.value, [finding.finding_id]: true };
  try {
    await apiPost(`/projects/${currentProject()}/sweeps:dismiss`, {
      theme: theme.value.trim(),
      finding_id: finding.finding_id,
      ref: finding.ref,
      locator: finding.locator,
      layer: finding.layer,
      operator: operator.value.trim(),
    });
    if (result.value) {
      result.value.hits = result.value.hits.filter((f) => f.finding_id !== finding.finding_id);
      result.value.review_suggested = result.value.review_suggested.filter(
        (f) => f.finding_id !== finding.finding_id,
      );
    }
    dismissedNow.value += 1;
    notifyOk("已排除，下次清查同一主题不再出现。");
  } catch (e) {
    notifyError(e);
  } finally {
    const next = { ...dismissing.value };
    delete next[finding.finding_id];
    dismissing.value = next;
  }
}
</script>

<template>
  <section>
    <PageHead overline="SWEEP" title="专项清查" purpose="按主题地毯式排查，生成可勾选的清单。" />
    <div class="pane form">
      <label class="field">
        <span class="label">要清查的主题或元素</span>
        <input v-model="theme" maxlength="200" :placeholder="`例如：${phTheme}`" />
      </label>
      <label class="field">
        <span class="label">关联词（可选，逗号分隔）</span>
        <input v-model="terms" placeholder="同义词、俗称、易漏写法" />
      </label>
      <label class="check">
        <input v-model="useJudge" type="checkbox" :disabled="!llmConfig().ready" />
        <span>
          用模型逐项判定（覆盖换了说法的内容）
          <i v-if="!llmConfig().ready" class="muted">— 先在「设置」接入模型</i>
        </span>
      </label>
      <div class="control-grid">
        <label class="field">
          <span class="label">模型判定上限</span>
          <input
            v-model.number="maxJudge"
            type="number"
            min="1"
            max="2000"
            step="1"
            :aria-invalid="!maxJudgeValid"
          />
          <small class="muted">
            仅在启用模型判定时生效；超出上限的对象不会丢弃，会保留为人工待查。
          </small>
          <small v-if="!maxJudgeValid" class="control-error">请输入 1–2000 的整数。</small>
        </label>
        <label class="field">
          <span class="label">语义相似阈值</span>
          <input
            v-model.number="semanticThreshold"
            type="number"
            min="0"
            max="1"
            step="0.01"
            :aria-invalid="!semanticThresholdValid"
          />
          <small class="muted">
            越低召回越宽；仅在真实语义模型可用时生效，结果会明确标出是否实际启用。
          </small>
          <small v-if="!semanticThresholdValid" class="control-error">请输入 0–1 的数值。</small>
        </label>
      </div>
      <button
        class="primary"
        :disabled="running || !theme.trim() || !maxJudgeValid || !semanticThresholdValid"
        @click="run"
      >
        {{ running ? "正在排查…" : "开始清查" }}
      </button>
    </div>

    <StepperProgress
      v-if="running || stageIndex >= 0"
      :stages="STAGES"
      :index="stageIndex"
      :running="running"
      :elapsed="elapsed"
      :hint="judgeNote"
      :flavors="FLAVORS"
    />

    <div v-if="result" class="pane done">
      <div class="chips">
        <span class="chip">扫描 <b>{{ result.scanned_total }}</b></span>
        <span class="chip red">直接命中 <b>{{ result.hits.length }}</b></span>
        <span class="chip amber">关联待查 <b>{{ result.review_suggested.length }}</b></span>
        <span v-if="result.semantic_used" class="chip gold">
          语义近似 <b>{{ result.semantic_flagged }} 项</b>
        </span>
        <span class="chip gold">
          语义判定 <b>{{ result.llm_used ? `${result.judged_count} 项` : "未启用" }}</b>
        </span>
        <span v-if="lastCost" class="chip">本次 <b>${{ lastCost.toFixed(4) }}</b></span>
      </div>
      <p v-if="result.judge_skipped" class="muted small">
        有 {{ result.judge_skipped }} 个对象超出单次判定上限，清单中已注明。
      </p>
      <p v-if="(result.dismissed_count || 0) + dismissedNow > 0" class="muted small">
        已排除 {{ (result.dismissed_count || 0) + dismissedNow }} 条误报——同一主题再清查时不再出现。
      </p>
      <p v-if="!result.hits.length && !result.review_suggested.length" class="ok-text">
        全库扫描完毕，未发现相关内容。
      </p>
      <template v-else>
        <label class="op-field">
          <span class="label">你的名字</span>
          <input v-model="operator" maxlength="80" placeholder="排除误报时记录谁做的决定" />
        </label>
        <TransitionGroup name="list" tag="div" class="findings">
          <div
            v-for="finding in visibleFindings"
            :key="finding.finding_id || finding.ref"
            class="finding"
            :class="{ review: finding.verdict === 'review' }"
          >
            <span class="badge">{{ finding.verdict === "hit" ? "待处理" : "建议复查" }}</span>
            <b>{{ finding.name }}</b>
            <span class="mono ref-cell">{{ finding.ref }}</span>
            <button
              class="dismiss"
              :disabled="dismissing[finding.finding_id]"
              title="标记为误报，下次清查同一主题不再出现"
              @click="dismiss(finding)"
            >
              {{ dismissing[finding.finding_id] ? "排除中…" : "排除" }}
            </button>
            <span v-if="finding.locator" class="locator mono">定位：{{ finding.locator }}</span>
            <span class="muted evidence">{{ finding.evidence }}</span>
          </div>
        </TransitionGroup>
        <button
          v-if="visibleFindingCount < allFindings.length"
          type="button"
          class="ghost"
          @click="visibleFindingCount += FINDING_PAGE_SIZE"
        >显示更多清查结果（剩余 {{ allFindings.length - visibleFindingCount }}）</button>
      </template>
      <button class="ghost" @click="downloadWorkOrder">导出清单 (.md)</button>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

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

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

input[type="text"],
input:not([type]) {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

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

.check i {
  font-style: normal;
  font-size: 0.78rem;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
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
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  font: inherit;
  font-size: 0.85rem;
  padding: 0.45rem 0.9rem;
  cursor: pointer;
  margin-top: 0.6rem;
}

.error {
  color: #e89a9a;
}

.ok-text {
  color: #8ed4ac;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.6rem;
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

.chip.gold {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

.findings {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.finding {
  display: grid;
  grid-template-columns: auto auto 1fr auto;
  gap: 0.3rem 0.7rem;
  align-items: baseline;
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  background: var(--ow-panel-2);
  padding: 0.5rem 0.75rem;
  font-size: 0.85rem;
}

.finding b {
  color: var(--ow-gold-bright);
}

.ref-cell {
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
}

.dismiss {
  align-self: center;
  background: transparent;
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.74rem;
  padding: 0.14rem 0.6rem;
  cursor: pointer;
  white-space: nowrap;
  transition: border-color 0.15s ease, color 0.15s ease;
}

.dismiss:hover:not(:disabled) {
  border-color: rgba(224, 133, 133, 0.5);
  color: #e89a9a;
}

.dismiss:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.locator {
  grid-column: 1 / -1;
  font-size: 0.74rem;
  color: var(--ow-gold-bright);
  opacity: 0.85;
}

.op-field {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  margin-bottom: 0.6rem;
}

.op-field .label {
  white-space: nowrap;
}

.op-field input {
  flex: 1;
  max-width: 22rem;
}

.finding.review .badge {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
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

.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.76rem;
}

.evidence {
  grid-column: 1 / -1;
  font-size: 0.8rem;
}

.list-enter-active {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.small {
  font-size: 0.78rem;
}

.control-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 0.8rem;
}

.control-grid input {
  width: 100%;
  min-height: 36px;
  box-sizing: border-box;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

.control-grid small {
  line-height: 1.45;
}

.control-error {
  color: #e89a9a;
}

@media (max-width: 600px) {
  .control-grid { grid-template-columns: 1fr; }
  .finding { grid-template-columns: 1fr; align-items: start; }
  .finding > * { min-width: 0; overflow-wrap: anywhere; }
  .dismiss { width: 100%; min-height: 44px; }
  .op-field { align-items: stretch; flex-direction: column; }
  .op-field input { width: 100%; max-width: none; }
}
</style>
