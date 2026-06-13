<script setup lang="ts">
import { onUnmounted, ref } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import {
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentProject,
  llmConfig,
  llmParams,
  streamJobEvents,
} from "../api";

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "scan", label: "词面扫描" },
  { key: "judge", label: "语义判定" },
  { key: "done", label: "工作单" },
];

interface Finding {
  ref: string;
  name: string;
  object_kind: string;
  layer: string;
  evidence: string;
  verdict: string;
}

interface SweepResult {
  scanned_total: number;
  llm_used: boolean;
  judged_count: number;
  judge_skipped: number;
  hits: Finding[];
  review_suggested: Finding[];
  markdown: string;
}

const theme = ref("");
const terms = ref("");
const useJudge = ref(llmConfig().ready);
const running = ref(false);
const stageIndex = ref(-1);
const elapsed = ref(0);
const judgeNote = ref("");
const error = ref("");
const result = ref<SweepResult | null>(null);
const lastCost = ref(0);

let timer: number | undefined;
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
});

async function run(): Promise<void> {
  if (!theme.value.trim() || running.value) return;
  running.value = true;
  stageIndex.value = 0;
  elapsed.value = 0;
  judgeNote.value = "";
  error.value = "";
  result.value = null;
  if (timer !== undefined) window.clearInterval(timer);
  timer = window.setInterval(() => {
    elapsed.value += 1;
  }, 1000);
  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${currentProject()}/jobs`, {
      kind: "theme_sweep",
      params: {
        theme: theme.value.trim(),
        extra_terms: terms.value
          .split(/[,，]/)
          .map((t) => t.trim())
          .filter(Boolean),
        use_llm: useJudge.value,
        ...(useJudge.value ? llmParams() : {}),
      },
    });
    stageIndex.value = 1;
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "judge") {
        stageIndex.value = 2;
        judgeNote.value = `已判定 ${event.data.done}/${event.data.total}`;
      } else if (event.type === "failed") {
        error.value = String(event.data.error ?? "任务失败");
      }
    });
    const status = await apiGet<{ status: string; result: SweepResult | null; error: string | null }>(
      `/jobs/${job.job_id}`,
    );
    if (status.status === "done" && status.result) {
      stageIndex.value = STAGES.length - 1;
      result.value = status.result;
      const used = costOf(status.result as { cost_budget?: { used_usd?: number } });
      lastCost.value = used;
      addSessionCost(used);
    } else if (!error.value) {
      error.value = status.error ?? "任务未完成";
    }
  } catch (e) {
    error.value = String(e);
  } finally {
    running.value = false;
    if (timer !== undefined) window.clearInterval(timer);
  }
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
</script>

<template>
  <section>
    <div class="section"><span class="t">专项清查</span></div>
    <p class="muted hint">
      限期消除某类主题？全库地毯式排查：词面命中、模型逐项判定（带原文证据）、关系扩散，产出可勾选的工作单。
    </p>
    <div class="pane form">
      <label class="field">
        <span class="label">要清查的主题或元素</span>
        <input v-model="theme" maxlength="200" placeholder="例如：赌博相关元素 / 某个被弃用的角色" />
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
      <button class="primary" :disabled="running || !theme.trim()" @click="run">
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
    />
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="result" class="pane done">
      <div class="chips">
        <span class="chip">扫描 <b>{{ result.scanned_total }}</b></span>
        <span class="chip red">直接命中 <b>{{ result.hits.length }}</b></span>
        <span class="chip amber">关联待查 <b>{{ result.review_suggested.length }}</b></span>
        <span class="chip gold">
          语义判定 <b>{{ result.llm_used ? `${result.judged_count} 项` : "未启用" }}</b>
        </span>
        <span v-if="lastCost" class="chip">本次 <b>${{ lastCost.toFixed(4) }}</b></span>
      </div>
      <p v-if="result.judge_skipped" class="muted small">
        有 {{ result.judge_skipped }} 个对象超出单次判定上限，工作单中已注明。
      </p>
      <p v-if="!result.hits.length && !result.review_suggested.length" class="ok-text">
        全库扫描完毕，未发现相关内容。
      </p>
      <TransitionGroup v-else name="list" tag="div" class="findings">
        <div
          v-for="finding in [...result.hits, ...result.review_suggested]"
          :key="finding.ref"
          class="finding"
          :class="{ review: finding.verdict === 'review' }"
        >
          <span class="badge">{{ finding.verdict === "hit" ? "待处理" : "建议复查" }}</span>
          <b>{{ finding.name }}</b>
          <span class="mono">{{ finding.ref }}</span>
          <span class="muted evidence">{{ finding.evidence }}</span>
        </div>
      </TransitionGroup>
      <button class="ghost" @click="downloadWorkOrder">导出工作单 (.md)</button>
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
  border-radius: 0.5rem;
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
  border-radius: 0.5rem;
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
  border-radius: 0.5rem;
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
  grid-template-columns: auto auto 1fr;
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

.finding.review .badge {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.badge {
  border: 1px solid rgba(224, 133, 133, 0.45);
  border-radius: 999px;
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
</style>
