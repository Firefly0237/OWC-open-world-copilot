<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from "vue";
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
  { key: "chunk", label: "分块提炼" },
  { key: "done", label: "候批" },
];

interface Gap {
  ref: string;
  object_ref: string;
  field: string;
  question: string;
  suggestion: string;
}
interface Beat {
  order: number;
  summary: string;
}
interface Draft {
  id: string;
  source_title: string;
  summary: string;
  plot_beats: Beat[];
  gaps: Gap[];
  stats: Record<string, number>;
  [k: string]: unknown;
}

const title = ref("");
const text = ref("");
const sourceKind = ref("文稿");
const running = ref(false);
const stageIndex = ref(-1);
const elapsed = ref(0);
const chunkNote = ref("");
const error = ref("");
const draft = ref<Draft | null>(null);
const answers = reactive<Record<string, string>>({});
const beatsAsQuests = ref(false);
const submitNote = ref("");
const submitting = ref(false);
const lastCost = ref(0);
const llmReady = ref(llmConfig().ready);

let timer: number | undefined;
function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}
onMounted(() => window.addEventListener("ow-llm-changed", onLlmChanged));
onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});

const STAT_LABEL: Record<string, string> = {
  entities: "实体",
  relations: "关系",
  beats: "节拍",
  terms: "术语",
  gaps: "缺口",
};

async function run(): Promise<void> {
  if (!title.value.trim() || !text.value.trim() || running.value || !llmReady.value) return;
  running.value = true;
  stageIndex.value = 0;
  elapsed.value = 0;
  chunkNote.value = "";
  error.value = "";
  draft.value = null;
  submitNote.value = "";
  if (timer !== undefined) window.clearInterval(timer);
  timer = window.setInterval(() => (elapsed.value += 1), 1000);
  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${currentProject()}/jobs`, {
      kind: "extraction",
      params: { title: title.value.trim(), text: text.value, source_kind: sourceKind.value.trim() || "文稿", ...llmParams() },
    });
    stageIndex.value = 1;
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "chunk") {
        chunkNote.value = `已提炼 ${event.data.index}/${event.data.total} 块`;
      } else if (event.type === "failed") {
        error.value = String(event.data.error ?? "任务失败");
      }
    });
    const status = await apiGet<{ status: string; result: { draft: Draft; cost_budget?: { used_usd?: number } } | null; error: string | null }>(
      `/jobs/${job.job_id}`,
    );
    if (status.status === "done" && status.result) {
      stageIndex.value = STAGES.length - 1;
      draft.value = status.result.draft;
      for (const g of draft.value.gaps) answers[g.ref] = g.suggestion ?? "";
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

async function submit(): Promise<void> {
  if (!draft.value || submitting.value) return;
  submitting.value = true;
  submitNote.value = "";
  error.value = "";
  try {
    const filled: Record<string, string> = {};
    for (const [ref, val] of Object.entries(answers)) if (val.trim()) filled[ref] = val.trim();
    const body = await apiPost<{ review_item_id: string; open_gaps: number; counts: Record<string, number> }>(
      `/projects/${currentProject()}/extractions:submit`,
      { draft: draft.value, answers: filled, include_beats_as_quests: beatsAsQuests.value },
    );
    const remaining = body.open_gaps;
    submitNote.value = `已入审阅台候批${remaining ? `（仍有 ${remaining} 处缺口未填，可在审阅时补）` : ""}。`;
  } catch (e) {
    error.value = String(e);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">文稿提炼 · 现成设定入库</span></div>
    <p class="muted hint">把现成的设定文档、章节、笔记粘进来，提炼成结构化的实体/关系/剧情节拍/术语；原文没讲清的，列成缺口让你补——不替你编。</p>

    <div class="pane form">
      <label class="field">
        <span class="label">来源标题</span>
        <input v-model="title" maxlength="200" placeholder="例如：雾铃群岛设定稿 v3" />
      </label>
      <label class="field inline">
        <span class="label">来源类型</span>
        <input v-model="sourceKind" maxlength="40" placeholder="文稿 / 章节 / 笔记…" />
      </label>
      <label class="field">
        <span class="label">正文 <i class="muted">{{ text.length }} 字</i></span>
        <textarea v-model="text" rows="8" placeholder="把设定文档整段粘进来。支持长文，会自动分块逐段提炼。"></textarea>
      </label>
      <button class="primary" :disabled="running || !title.trim() || !text.trim() || !llmReady" @click="run">
        {{ running ? "提炼中…" : "开始提炼" }}
      </button>
      <p v-if="!llmReady" class="muted small">提炼走真实模型——先在 <RouterLink to="/settings" class="golink">设置</RouterLink> 接入。</p>
    </div>

    <StepperProgress v-if="running || stageIndex >= 0" :stages="STAGES" :index="stageIndex" :running="running" :elapsed="elapsed" :hint="chunkNote" />
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="draft" class="pane done">
      <div class="r-head">
        <b>{{ draft.source_title }}</b>
        <span v-if="lastCost" class="cost">${{ lastCost.toFixed(4) }}</span>
      </div>
      <p v-if="draft.summary" class="summary">{{ draft.summary }}</p>
      <div class="chips">
        <span v-for="(v, k) in draft.stats" :key="k" class="chip">{{ STAT_LABEL[k] ?? k }} <b>{{ v }}</b></span>
      </div>

      <template v-if="draft.plot_beats.length">
        <div class="section sub"><span class="t">剧情节拍</span></div>
        <ol class="beats">
          <li v-for="b in draft.plot_beats" :key="b.order">{{ b.summary }}</li>
        </ol>
      </template>

      <template v-if="draft.gaps.length">
        <div class="section sub"><span class="t">缺口（原文没讲清，你来定）</span></div>
        <div class="gaps">
          <label v-for="g in draft.gaps" :key="g.ref" class="gap">
            <span class="q">{{ g.question }}</span>
            <input v-model="answers[g.ref]" :placeholder="g.suggestion ? `模型猜测：${g.suggestion}` : '留空则审阅时再补'" />
          </label>
        </div>
      </template>

      <label class="check">
        <input v-model="beatsAsQuests" type="checkbox" />
        <span>把剧情节拍一并转成任务草稿</span>
      </label>
      <button class="primary" :disabled="submitting" @click="submit">
        {{ submitting ? "提交中…" : "送入审阅台" }}
      </button>
      <p v-if="submitNote" class="flash">{{ submitNote }}</p>
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
  gap: 0.8rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field.inline {
  flex-direction: row;
  align-items: center;
  gap: 0.7rem;
}

.field.inline input {
  width: 12rem;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

.label i {
  font-style: normal;
  margin-left: 0.4rem;
  font-size: 0.74rem;
}

textarea,
input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

textarea {
  resize: vertical;
}

textarea:focus,
input:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
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

.small {
  font-size: 0.78rem;
}

.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.error {
  color: #e89a9a;
}

.flash {
  color: #8ed4ac;
  margin-top: 0.5rem;
}

.done {
  margin-top: 0.9rem;
  padding: 0.95rem 1.15rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.r-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
}

.r-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  color: var(--ow-cyan);
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
}

.summary {
  margin: 0;
  line-height: 1.65;
}

.chips {
  display: flex;
  flex-wrap: wrap;
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

.sub {
  margin-top: 0.4rem;
}

.beats {
  margin: 0;
  padding-left: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.86rem;
  line-height: 1.5;
}

.gaps {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.gap {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}

.gap .q {
  font-size: 0.84rem;
  color: var(--ow-ink);
}

.check {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  font-size: 0.85rem;
}
</style>
