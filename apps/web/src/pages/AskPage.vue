<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
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
  grounded?: boolean;
  grounding_level?: string;
  retrieval_degraded?: boolean;
  retrieval_degradation_reasons?: string[];
  citations?: AskCitation[];
  faithfulness?: {
    judge: string;
    ok: boolean;
    score: number | null;
    unsupported_claims: string[];
    note: string;
  } | null;
}

interface AskCitation {
  ref: string;
  source_title?: string;
  source_preview?: string;
  page?: number | null;
  bbox?: number[] | null;
  revision?: string;
  locator?: Record<string, unknown> | null;
  supported_claims?: string[];
}

interface Turn {
  question: string;
  answer: string;
  refused: boolean;
  citations: AskCitation[];
  cost: number;
  faithfulness: AskAnswer["faithfulness"];
  groundingLevel: string;
  degraded: boolean;
  degradationReasons: string[];
}

interface ContextHit {
  ref: string;
  title: string;
  body: string;
  score: number;
  source: string;
}

interface ContextPackPreview {
  query: string;
  refs: string[];
  hits: ContextHit[];
  degraded: boolean;
  degradationReasons: string[];
}

const question = ref("");
const turns = ref<Turn[]>([]);
const busy = ref(false);
const llmReady = ref(llmConfig().ready);
const indexing = ref(false);
const overviewMsg = ref("");
const agentic = ref(false);
const faithfulness = ref<"off" | "offline" | "llm">("off");
const contextBudget = ref(800);
const contextBusy = ref(false);
const contextPack = ref<ContextPackPreview | null>(null);

function degradationLabel(reason: string): string {
  const labels: Record<string, string> = {
    sentence_transformers_not_installed: "未安装 sentence-transformers，向量召回使用哈希基线",
    embedder_degraded: "语义嵌入器已降级",
    neural_reranker_degraded: "神经重排已降级为词法重排",
  };
  if (labels[reason]) return labels[reason];
  if (reason.startsWith("semantic_model_load_failed:")) return `语义模型加载失败（${reason.split(":")[1]}）`;
  if (reason.startsWith("neural_reranker_")) return `神经重排失败（${reason.split(":").slice(1).join(":")}）`;
  if (reason.startsWith("agentic_")) return `Agentic 检索组件回退（${reason}）`;
  return reason;
}

function locatorLabel(citation: AskCitation): string {
  if (citation.page) return `第 ${citation.page} 页`;
  const kind = citation.locator?.kind;
  return typeof kind === "string" ? kind : "";
}

function normalizedContextBudget(): number | null {
  const value = Number(contextBudget.value);
  return Number.isInteger(value) && value >= 1 && value <= 8000 ? value : null;
}

const contextBudgetValid = computed(() => normalizedContextBudget() !== null);

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

async function previewContextPack(): Promise<void> {
  const query = question.value.trim();
  const budget = normalizedContextBudget();
  if (!query || contextBusy.value || budget === null) return;
  contextBusy.value = true;
  try {
    const body = await apiPost<{
      refs: string[];
      hits: ContextHit[];
      degraded?: boolean;
      degradation_reasons?: string[];
    }>(
      `/projects/${currentProject()}/context:pack`,
      { query, budget_tokens: budget },
    );
    contextPack.value = {
      query,
      refs: body.refs,
      hits: body.hits,
      degraded: body.degraded ?? false,
      degradationReasons: body.degradation_reasons ?? [],
    };
  } catch (e) {
    notifyError(e);
  } finally {
    contextBusy.value = false;
  }
}

async function ask(): Promise<void> {
  const q = question.value.trim();
  const budget = normalizedContextBudget();
  if (!q || busy.value || !llmReady.value || budget === null) return;
  busy.value = true;
  try {
    const body = await apiPost<{
      answer: AskAnswer;
      degraded?: boolean;
      degradation_reasons?: string[];
      cost_budget?: { used_usd?: number };
    }>(
      `/projects/${currentProject()}/ask`,
      {
        query: q,
        ...llmParams(),
        budget_tokens: budget,
        agentic: agentic.value,
        faithfulness: faithfulness.value,
      },
    );
    const used = costOf(body);
    addSessionCost(used);
    turns.value.push({
      question: q,
      // The backend supplies an honest, case-specific message on refusal (nothing relevant exists
      // vs. relevant lore found but the asked-for point isn't recorded), so surface it directly.
      answer: body.answer.answer,
      refused: body.answer.refused,
      citations: body.answer.citations ?? [],
      cost: used,
      faithfulness: body.answer.faithfulness,
      groundingLevel: body.answer.grounding_level ?? "none",
      degraded: body.degraded ?? body.answer.retrieval_degraded ?? false,
      degradationReasons:
        body.degradation_reasons ?? body.answer.retrieval_degradation_reasons ?? [],
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
    <div class="retrieval-options pane">
      <label>
        <input v-model="agentic" type="checkbox" />
        <span>Agentic RAG：证据不足时自动评估并补检一次</span>
      </label>
      <label>
        <span>答案忠实性核验</span>
        <select v-model="faithfulness">
          <option value="off">关闭</option>
          <option value="offline">确定性离线核验（$0）</option>
          <option value="llm">模型蕴含核验（需真实模型）</option>
        </select>
      </label>
      <label>
        <span>证据包预算</span>
        <input
          v-model.number="contextBudget"
          type="number"
          min="1"
          max="8000"
          step="1"
          :aria-invalid="!contextBudgetValid"
        />
        <span>tokens</span>
      </label>
      <span v-if="!contextBudgetValid" class="control-error">请输入 1–8000 的整数预算。</span>
      <button
        class="ghost"
        :disabled="contextBusy || !question.trim() || !contextBudgetValid"
        @click="previewContextPack"
      >
        {{ contextBusy ? "检索中…" : "预览原始证据包" }}
      </button>
    </div>
    <div v-if="contextPack" class="pane context-pack reveal">
      <div class="context-head">
        <b>原始上下文证据包</b>
        <span class="muted small">“{{ contextPack.query }}” · {{ contextPack.hits.length }} 条</span>
      </div>
      <div v-if="contextPack.degraded" class="degradation" role="status">
        <b>检索已降级，结果仍可用但语义能力受限。</b>
        <span v-for="reason in contextPack.degradationReasons" :key="reason">
          {{ degradationLabel(reason) }}
        </span>
      </div>
      <p v-if="!contextPack.hits.length" class="muted small">当前预算内没有检索到相关正典证据。</p>
      <details v-for="hit in contextPack.hits" v-else :key="hit.ref" class="context-hit">
        <summary>
          <span>{{ hit.title }}</span>
          <code>{{ hit.ref }}</code>
          <i>{{ hit.score.toFixed(3) }} · {{ hit.source }}</i>
        </summary>
        <pre>{{ hit.body }}</pre>
      </details>
    </div>
    <div class="thread">
      <TransitionGroup v-if="turns.length" name="turn" tag="div">
        <div v-for="(turn, index) in turns" :key="index" class="turn">
          <div class="pane q">{{ turn.question }}</div>
          <div class="pane a" :class="{ refused: turn.refused }">
            <p>{{ turn.answer }}</p>
            <div v-if="turn.degraded" class="degradation" role="status">
              <b>本次检索使用了回退路径。</b>
              <span v-for="reason in turn.degradationReasons" :key="reason">
                {{ degradationLabel(reason) }}
              </span>
            </div>
            <div v-if="turn.citations.length || turn.cost" class="chips">
              <details v-for="citation in turn.citations" :key="citation.ref" class="citation">
                <summary class="chip">
                  {{ citation.ref }}
                  <span v-if="locatorLabel(citation)"> · {{ locatorLabel(citation) }}</span>
                </summary>
                <div class="citation-source">
                  <b>{{ citation.source_title || citation.ref }}</b>
                  <p>{{ citation.source_preview || "该旧引用没有可用的原文预览。" }}</p>
                  <small v-if="citation.revision">版本：{{ citation.revision }}</small>
                  <small v-if="citation.bbox?.length === 4">bbox：{{ citation.bbox.join(", ") }}</small>
                  <ul v-if="citation.supported_claims?.length">
                    <li v-for="claim in citation.supported_claims" :key="claim">支持：{{ claim }}</li>
                  </ul>
                </div>
              </details>
              <span v-if="turn.cost" class="chip dim">${{ turn.cost.toFixed(4) }}</span>
            </div>
            <p v-if="turn.groundingLevel === 'citation_existence_only'" class="grounding-note">
              当前仅验证了引用存在；开启忠实性核验可检查论断是否真被原文支持。
            </p>
            <div
              v-if="turn.faithfulness"
              class="faithfulness"
              :class="{ failed: !turn.faithfulness.ok }"
            >
              <b>
                忠实性
                {{ turn.faithfulness.score == null ? "未完成" : `${Math.round(turn.faithfulness.score * 100)}%` }}
              </b>
              <span>{{ turn.faithfulness.note }}</span>
              <details v-if="turn.faithfulness.unsupported_claims.length">
                <summary>查看未获证据支持的论断</summary>
                <ul>
                  <li v-for="claim in turn.faithfulness.unsupported_claims" :key="claim">{{ claim }}</li>
                </ul>
              </details>
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
      <button
        class="primary"
        :disabled="busy || !question.trim() || !llmReady || !contextBudgetValid"
        @click="ask"
      >
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

.citation {
  position: relative;
}

.citation summary {
  cursor: pointer;
  list-style: none;
}

.citation-source {
  display: grid;
  gap: 0.25rem;
  max-width: min(42rem, 80vw);
  margin-top: 0.35rem;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  font-size: 0.75rem;
}

.citation-source p,
.citation-source ul {
  margin: 0;
}

.citation-source small {
  color: var(--ow-muted);
}

.degradation {
  display: grid;
  gap: 0.15rem;
  margin: 0.45rem 0;
  padding: 0.5rem 0.65rem;
  border-left: 2px solid #e6c07e;
  background: rgba(230, 192, 126, 0.08);
  color: var(--ow-muted);
  font-size: 0.76rem;
}

.degradation b {
  color: var(--ow-ink);
}

.grounding-note {
  color: var(--ow-muted);
  font-size: 0.74rem;
}

.retrieval-options {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.7rem 1.2rem;
  margin-bottom: 0.8rem;
  padding: 0.65rem 0.8rem;
}

.retrieval-options label {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  min-height: 36px;
  color: var(--ow-muted);
  font-size: 0.78rem;
}

.retrieval-options select {
  min-height: 36px;
  padding: 0.35rem 0.55rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  background: var(--ow-panel-2);
}

.retrieval-options input[type="number"] {
  width: 6rem;
  min-height: 36px;
  padding: 0.35rem 0.5rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  background: var(--ow-panel-2);
}

.control-error {
  color: #e89a9a;
  font-size: 0.76rem;
}

.context-pack {
  margin-bottom: 0.8rem;
  padding: 0.75rem 0.85rem;
}

.context-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 0.7rem;
  margin-bottom: 0.4rem;
}

.context-head b,
.context-hit summary span {
  color: var(--ow-gold-bright);
}

.context-hit {
  border-top: 1px solid var(--ow-line);
  padding: 0.45rem 0;
}

.context-hit summary {
  display: flex;
  align-items: baseline;
  gap: 0.55rem;
  min-height: 32px;
  cursor: pointer;
  font-size: 0.8rem;
}

.context-hit summary code {
  color: var(--ow-cyan);
}

.context-hit summary i {
  margin-left: auto;
  color: var(--ow-muted);
  font-style: normal;
  font-size: 0.72rem;
}

.context-hit pre {
  max-height: 15rem;
  overflow: auto;
  margin: 0.35rem 0 0;
  padding: 0.55rem;
  white-space: pre-wrap;
  color: var(--ow-ink);
  background: var(--ow-panel-2);
  font: 0.75rem/1.55 ui-monospace, Consolas, monospace;
}

.faithfulness {
  display: grid;
  gap: 0.25rem;
  margin-top: 0.6rem;
  padding: 0.55rem 0.65rem;
  border-left: 2px solid #8ed4ac;
  color: var(--ow-muted);
  background: rgba(142, 212, 172, 0.06);
  font-size: 0.76rem;
}

.faithfulness.failed {
  border-left-color: #e6c07e;
  background: rgba(230, 192, 126, 0.07);
}

.faithfulness b { color: var(--ow-ink); }
.faithfulness summary { min-height: 32px; cursor: pointer; color: var(--ow-gold-bright); }
.faithfulness ul { margin: 0.25rem 0 0; padding-left: 1.1rem; }

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
