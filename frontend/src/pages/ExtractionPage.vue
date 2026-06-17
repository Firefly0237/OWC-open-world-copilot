<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref, toRef, toRefs } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import PageHead from "../components/PageHead.vue";
import { apiGet, apiPost, currentProject, llmConfig, llmParams } from "../api";
import { getJobChannel, startJob } from "../jobs";
import { notifyError } from "../toast";

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "chunk", label: "分块提炼" },
  { key: "done", label: "候批" },
];

const FLAVORS: Record<string, string> = {
  accepted: "受理文稿…",
  chunk: "逐段读出实体、关系与节拍…",
  done: "提炼完成。",
};

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
interface Unsupported {
  ref: string;
  name: string;
  kind: string;
  reason?: string;
  detail?: string;
  source_check?: string;
}
interface Coverage {
  granularity: string; // full | coarsened | partial
  total_chars: number;
  covered_chars: number;
  language: string;
  languages: string[];
  mixed: boolean;
  note: string;
}
interface Draft {
  id: string;
  source_title: string;
  summary: string;
  plot_beats: Beat[];
  gaps: Gap[];
  unsupported: Unsupported[];
  coverage?: Coverage | null;
  stats: Record<string, number>;
  [k: string]: unknown;
}

const title = ref("");
const text = ref("");
const sourceKind = ref("文稿");

const job = getJobChannel<Draft>("extraction", STAGES);
const { running, stageIndex, elapsed, result: draft } = toRefs(job);
const chunkNote = toRef(job, "hint");
const lastCost = toRef(job, "cost");

const answers = reactive<Record<string, string>>({});
const verifyFaithfulness = ref(false);
const beatsAsQuests = ref(false);
const submitNote = ref("");
const submitting = ref(false);
const llmReady = ref(llmConfig().ready);

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}
onMounted(() => window.addEventListener("ow-llm-changed", onLlmChanged));
onUnmounted(() => window.removeEventListener("ow-llm-changed", onLlmChanged));

const STAT_LABEL: Record<string, string> = {
  entities: "实体",
  relations: "关系",
  beats: "节拍",
  terms: "术语",
  gaps: "缺口",
  unsupported: "原文未见",
};

async function run(): Promise<void> {
  if (!title.value.trim() || !text.value.trim() || running.value || !llmReady.value) return;
  submitNote.value = "";
  await startJob<Draft>("extraction", {
    kind: "extraction",
    stages: STAGES,
    params: {
      title: title.value.trim(),
      text: text.value,
      source_kind: sourceKind.value.trim() || "文稿",
      verify_faithfulness: verifyFaithfulness.value,
      ...llmParams(),
    },
    onEvent: (ch, event) => {
      if (event.type === "chunk") {
        if (ch.stageIndex < 1) ch.stageIndex = 1;
        ch.hint = `已提炼 ${event.data.index}/${event.data.total} 块`;
      }
    },
    parseResult: (raw) => {
      const d = (raw as { draft: Draft }).draft;
      for (const g of d.gaps) answers[g.ref] = g.suggestion ?? "";
      return d;
    },
  });
}

async function submit(): Promise<void> {
  if (!draft.value || submitting.value) return;
  submitting.value = true;
  submitNote.value = "";
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
    notifyError(e);
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="EXTRACTION" title="文稿提炼" purpose="把现成文稿整理成结构化设定，长篇与多语言都接得住。" />

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
        <textarea v-model="text" rows="8" placeholder="把设定文档整段粘进来，长篇与多语言都支持。"></textarea>
      </label>
      <label class="check">
        <input v-model="verifyFaithfulness" type="checkbox" />
        <span>校验关系真实性<i class="muted">逐条核对提炼出的关系是否真有原文依据，多一次模型调用</i></span>
      </label>
      <button class="primary" :disabled="running || !title.trim() || !text.trim() || !llmReady" @click="run">
        {{ running ? "提炼中…" : "开始提炼" }}
      </button>
      <p v-if="!llmReady" class="muted small">请先在 <RouterLink to="/settings" class="golink">设置</RouterLink> 里接入模型。</p>
    </div>

    <StepperProgress v-if="running || stageIndex >= 0" :stages="STAGES" :index="stageIndex" :running="running" :elapsed="elapsed" :hint="chunkNote" :flavors="FLAVORS" />

    <div v-if="draft" class="pane done">
      <div class="r-head">
        <b>{{ draft.source_title }}</b>
        <span v-if="lastCost" class="cost">${{ lastCost.toFixed(4) }}</span>
      </div>
      <p v-if="draft.summary" class="summary">{{ draft.summary }}</p>
      <p
        v-if="draft.coverage && draft.coverage.granularity === 'partial'"
        class="warn"
      >{{ draft.coverage.note }}</p>
      <p v-else-if="draft.coverage && draft.coverage.note" class="coverage muted">
        ✦ {{ draft.coverage.note }}
      </p>
      <div class="chips">
        <span v-for="(v, k) in draft.stats" :key="k" class="chip">{{ STAT_LABEL[k] ?? k }} <b>{{ v }}</b></span>
      </div>

      <div v-if="draft.unsupported && draft.unsupported.length" class="warn unsupported">
        <b>原文未充分支持，请核对后再采纳：</b>
        <ul>
          <li v-for="(u, i) in draft.unsupported" :key="i">
            <span class="u-kind">{{ u.kind === "relation" ? "关系" : u.kind === "term" ? "术语" : "名称" }}</span>
            {{ u.detail || u.name }}
            <i v-if="u.source_check === 'llm'" class="u-tag">模型判定</i>
          </li>
        </ul>
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

.coverage {
  margin: 0;
  font-size: 0.8rem;
  line-height: 1.55;
}

.check i {
  font-style: normal;
  font-size: 0.78rem;
  margin-left: 0.4rem;
}

.unsupported ul {
  margin: 0.35rem 0 0;
  padding-left: 0;
  list-style: none;
  display: flex;
  flex-direction: column;
  gap: 0.28rem;
}
.unsupported li {
  font-size: 0.84rem;
  line-height: 1.55;
}
.u-kind {
  display: inline-block;
  font-size: 0.7rem;
  border: 1px solid currentColor;
  border-radius: 999px;
  padding: 0 0.4rem;
  margin-right: 0.4rem;
  opacity: 0.85;
}
.u-tag {
  font-style: normal;
  font-size: 0.7rem;
  color: var(--ow-violet);
  margin-left: 0.4rem;
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
