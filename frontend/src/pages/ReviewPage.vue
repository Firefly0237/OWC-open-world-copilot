<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  apiGet,
  apiPost,
  currentOperator,
  currentProject,
  setCurrentOperator,
} from "../api";

interface ReviewItem {
  id: string;
  item_type: string;
  object_ref: string;
  payload: Record<string, unknown>;
}

const items = ref<ReviewItem[]>([]);
const operator = ref(currentOperator());
const flash = ref("");
const error = ref("");

const TYPE_LABELS: Record<string, string> = {
  quest_draft: "任务草稿",
  bark_variant: "台词变体",
  patch_candidate: "修复补丁",
  world_seed: "世界草案",
  import_draft: "提炼草案",
  dialogue_tree: "对话树",
  flavor_batch: "物案批次",
  character_profile: "角色卡",
};

function summarize(item: ReviewItem): string {
  const payload = item.payload;
  const summary = payload["summary"] ?? payload["title"] ?? payload["text"] ?? "";
  return String(summary).slice(0, 160);
}

async function refresh(): Promise<void> {
  error.value = "";
  try {
    const body = await apiGet<{ items: ReviewItem[] }>(
      `/projects/${currentProject()}/review_items`,
    );
    items.value = body.items;
  } catch (e) {
    error.value = String(e);
  }
}

async function decide(item: ReviewItem, decision: "accepted" | "rejected"): Promise<void> {
  if (!operator.value.trim()) {
    error.value = "先填署名。";
    return;
  }
  setCurrentOperator(operator.value.trim());
  flash.value = "";
  error.value = "";
  try {
    const body = await apiPost<{ written_ref: string | null; decision: string }>(
      `/projects/${currentProject()}/review_items/${item.id}:decide`,
      { decision, operator: operator.value.trim() },
    );
    flash.value =
      decision === "accepted"
        ? `已钤印入档${body.written_ref ? `：${body.written_ref}` : ""}。`
        : "已驳回，草稿就地焚毁。";
  } catch (e) {
    // 409 = decided elsewhere meanwhile: decisions are final, just resync
    error.value = String(e);
  }
  await refresh();
}

onMounted(refresh);
</script>

<template>
  <section>
    <div class="section"><span class="t">审阅台 · 你执朱笔</span></div>
    <p class="muted hint">草稿在此候批：采纳入档，驳回焚稿，不可反悔。</p>
    <div class="operator">
      <input v-model="operator" placeholder="署名（必填）" />
      <button @click="refresh">刷新队列</button>
    </div>
    <p v-if="flash" class="flash">{{ flash }}</p>
    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="!items.length && !error" class="muted">案头清净——暂无候批的草稿。</p>
    <TransitionGroup name="card" tag="div" class="queue">
      <div v-for="item in items" :key="item.id" class="pane card">
        <div class="head">
          <span class="type">{{ TYPE_LABELS[item.item_type] ?? item.item_type }}</span>
          <span class="mono">{{ item.object_ref }}</span>
        </div>
        <p class="muted body">{{ summarize(item) }}</p>
        <div class="actions">
          <button class="primary" @click="decide(item, 'accepted')">采纳</button>
          <button @click="decide(item, 'rejected')">驳回</button>
        </div>
      </div>
    </TransitionGroup>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.operator {
  display: flex;
  gap: 0.5rem;
  margin-bottom: 0.9rem;
}

.operator input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
  width: 14rem;
}

button {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.9rem;
  cursor: pointer;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}

.card {
  padding: 0.8rem 1rem;
  margin-bottom: 0.7rem;
}

.head {
  display: flex;
  gap: 0.7rem;
  align-items: baseline;
}

.type {
  color: var(--ow-gold-bright);
  font-weight: 600;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.78rem;
}

.body {
  margin: 0.4rem 0 0.6rem;
  font-size: 0.85rem;
}

.actions {
  display: flex;
  gap: 0.5rem;
}

.flash {
  color: #8ed4ac;
}

.error {
  color: #e89a9a;
}

.queue {
  position: relative;
}

/* decided cards sweep off the desk; the queue closes ranks */
.card-enter-active,
.card-leave-active,
.card-move {
  transition:
    opacity 0.35s ease,
    transform 0.35s ease;
}

.card-enter-from {
  opacity: 0;
  transform: translateY(8px);
}

.card-leave-to {
  opacity: 0;
  transform: translateX(24px);
}

.card-leave-active {
  position: absolute;
  width: 100%;
}

@media (prefers-reduced-motion: reduce) {
  .card-enter-active,
  .card-leave-active,
  .card-move {
    transition: none;
  }
}
</style>
