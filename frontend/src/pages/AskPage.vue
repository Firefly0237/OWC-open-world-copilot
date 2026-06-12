<script setup lang="ts">
import { ref } from "vue";
import { apiPost, currentProject } from "../api";

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
}

const question = ref("");
const turns = ref<Turn[]>([]);
const busy = ref(false);
const error = ref("");

const REFUSAL_TEXT = "档案中查无此条——我不杜撰。";

async function ask(): Promise<void> {
  const q = question.value.trim();
  if (!q || busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    const body = await apiPost<{ answer: AskAnswer }>(
      `/projects/${currentProject()}/ask`,
      { query: q },
    );
    turns.value.push({
      question: q,
      answer: body.answer.refused ? REFUSAL_TEXT : body.answer.answer,
      refused: body.answer.refused,
      citations: (body.answer.citations ?? []).map((c) => c.ref),
    });
    question.value = "";
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">世界问答</span></div>
    <p class="muted hint">有问必有据；查无此条，绝不杜撰。</p>
    <div v-for="(turn, index) in turns" :key="index" class="turn">
      <div class="pane q">{{ turn.question }}</div>
      <div class="pane a" :class="{ refused: turn.refused }">
        <p>{{ turn.answer }}</p>
        <div v-if="turn.citations.length" class="chips">
          <span v-for="ref in turn.citations" :key="ref" class="chip">{{ ref }}</span>
        </div>
      </div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div class="composer">
      <input
        v-model="question"
        placeholder="向你的世界提问……"
        @keydown.enter="ask"
      />
      <button class="primary" :disabled="busy || !question.trim()" @click="ask">
        {{ busy ? "翻阅中…" : "提问" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
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
  border-radius: 999px;
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
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.55rem 0.75rem;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: 0.5rem;
  color: #241a05;
  font-weight: 600;
  padding: 0.5rem 1.1rem;
  cursor: pointer;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.error {
  color: #e89a9a;
}
</style>
