<script setup lang="ts">
import { onMounted, onUnmounted, ref } from "vue";
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
import { notifyError } from "../toast";
import PageHead from "../components/PageHead.vue";
import EmptyState from "../components/EmptyState.vue";
import { example } from "../examples";

const phQuery = example("askQuery");

interface AskAnswer {
  answer: string;
  refused: boolean;
  citations?: { ref: string }[];
}

interface Turn {
  question: string;
  answer: string;
  refused: boolean;
  citations: string[];
  cost: number;
}

const question = ref("");
const turns = ref<Turn[]>([]);
const busy = ref(false);
const llmReady = ref(llmConfig().ready);
const indexing = ref(false);
const overviewMsg = ref("");

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}

onMounted(() => window.addEventListener("ow-llm-changed", onLlmChanged));
onUnmounted(() => window.removeEventListener("ow-llm-changed", onLlmChanged));

async function buildOverview(): Promise<void> {
  if (indexing.value || !llmReady.value) return;
  indexing.value = true;
  overviewMsg.value = "";
  try {
    const { job_id } = await apiPost<{ job_id: string }>(
      `/projects/${currentProject()}/jobs`,
      { kind: "build_overview", params: { ...llmParams() } },
    );
    await streamJobEvents(job_id, () => {});
    const job = await apiGet<{
      status: string;
      result?: { community_count?: number; regenerated?: number };
    }>(`/jobs/${job_id}`);
    if (job.status !== "done") throw new Error("总览索引构建失败");
    const n = job.result?.community_count ?? 0;
    const fresh = job.result?.regenerated ?? 0;
    overviewMsg.value = `世界总览已就绪：${n} 个聚类（本次新写 ${fresh} 份摘要）。现在可以问宏观格局类问题了。`;
  } catch (e) {
    notifyError(e);
  } finally {
    indexing.value = false;
  }
}

async function ask(): Promise<void> {
  const q = question.value.trim();
  if (!q || busy.value || !llmReady.value) return;
  busy.value = true;
  try {
    const body = await apiPost<{ answer: AskAnswer; cost_budget?: { used_usd?: number } }>(
      `/projects/${currentProject()}/ask`,
      { query: q, ...llmParams() },
    );
    const used = costOf(body);
    addSessionCost(used);
    turns.value.push({
      question: q,
      // The backend supplies an honest, case-specific message on refusal (nothing relevant exists
      // vs. relevant lore found but the asked-for point isn't recorded), so surface it directly.
      answer: body.answer.answer,
      refused: body.answer.refused,
      citations: (body.answer.citations ?? []).map((c) => c.ref),
      cost: used,
    });
    question.value = "";
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="ORACLE" title="世界问答" purpose="就世界设定提问，答案附依据，查不到会直说。" />
    <div class="overview">
      <button class="ghost" :disabled="indexing || !llmReady" @click="buildOverview">
        {{ indexing ? "构建中…" : "构建世界总览索引" }}
      </button>
      <span class="muted small">宏观格局类问题需先建一次总览，世界改动后可重建。</span>
    </div>
    <p v-if="overviewMsg" class="flash">{{ overviewMsg }}</p>
    <div class="thread">
      <TransitionGroup v-if="turns.length" name="turn" tag="div">
        <div v-for="(turn, index) in turns" :key="index" class="turn">
          <div class="pane q">{{ turn.question }}</div>
          <div class="pane a" :class="{ refused: turn.refused }">
            <p>{{ turn.answer }}</p>
            <div v-if="turn.citations.length || turn.cost" class="chips">
              <span v-for="ref in turn.citations" :key="ref" class="chip">{{ ref }}</span>
              <span v-if="turn.cost" class="chip dim">${{ turn.cost.toFixed(4) }}</span>
            </div>
          </div>
        </div>
      </TransitionGroup>
      <EmptyState
        v-else
        :busy="busy"
        :title="busy ? '翻阅档案中' : '世界问答'"
        :hint="busy ? '' : '在下方输入问题，向世界发问'"
      />
    </div>
    <div class="composer">
      <input
        v-model="question"
        :placeholder="`例如：${phQuery}`"
        @keydown.enter="ask"
      />
      <button class="primary" :disabled="busy || !question.trim() || !llmReady" @click="ask">
        {{ busy ? "翻阅中…" : "提问" }}
      </button>
    </div>
    <p v-if="!llmReady" class="muted small">
      请先在
      <RouterLink to="/settings" class="golink">设置</RouterLink>
      接入模型。
    </p>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

/* the conversation fills the vertical space so the page never reads as a lone composer floating in
   a void — empty it shows the standing-by panel, with content it grows naturally. */
.thread {
  min-height: clamp(280px, 44vh, 520px);
  display: flex;
  flex-direction: column;
  justify-content: flex-start;
}

.turn {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  margin-bottom: 0.9rem;
}

.q {
  align-self: flex-end;
  max-width: 80%;
  padding: 0.55rem 0.85rem;
  border-color: rgba(143, 214, 232, 0.35);
}

.a {
  align-self: flex-start;
  max-width: 90%;
  padding: 0.65rem 0.9rem;
}

.a.refused {
  border-color: rgba(224, 180, 106, 0.45);
}

.a p {
  margin: 0 0 0.4rem;
  line-height: 1.7;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}

.chip {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  border-radius: 3px;
  color: var(--ow-gold-bright);
  font-size: 0.74rem;
  padding: 0.1rem 0.55rem;
  font-family: ui-monospace, Consolas, monospace;
}

.composer {
  display: flex;
  gap: 0.5rem;
  margin-top: 1rem;
}

.composer input {
  flex: 1;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.55rem 0.75rem;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
  color: #241a05;
  font-weight: 600;
  padding: 0.5rem 1.1rem;
  cursor: pointer;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.overview {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  flex-wrap: wrap;
  margin: 0.4rem 0 0.6rem;
}

button.ghost {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-gold-soft);
  border-radius: var(--ow-control-radius);
  color: var(--ow-gold-bright);
  padding: 0.35rem 0.8rem;
  cursor: pointer;
  white-space: nowrap;
}

button.ghost:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.flash {
  color: #8ed4ac;
}

.error {
  color: #e89a9a;
}

.chip.dim {
  border-color: rgba(143, 214, 232, 0.35);
  background: transparent;
  color: var(--ow-cyan);
}

.small {
  font-size: 0.78rem;
  margin-top: 0.5rem;
}

.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.turn-enter-active {
  transition:
    opacity 0.35s ease,
    transform 0.35s ease;
}

.turn-enter-from {
  opacity: 0;
  transform: translateY(8px);
}
</style>
