<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { apiGet, apiPost, currentProject } from "../api";
import PageHead from "../components/PageHead.vue";
import { notifyError } from "../toast";

const CHANGE_TYPES = [
  { value: "entity_delete", label: "删除实体" },
  { value: "entity_rename", label: "重命名实体" },
  { value: "entity_field_change", label: "修改实体字段" },
  { value: "relation_change", label: "改动关系" },
  { value: "content_change", label: "改动内容" },
];

interface ArchiveEntity {
  id: string;
  name: string;
  type: string;
}

interface ImpactItem {
  target_ref: string;
  level: string;
  distance: number;
  reason: string;
  source_change: string;
}

const entities = ref<ArchiveEntity[]>([]);
const nameByRef = ref<Record<string, string>>({});
const changes = reactive<{ change_type: string; target_ref: string }[]>([
  { change_type: "entity_delete", target_ref: "" },
]);
const maxDepth = ref(2);
const running = ref(false);
const result = ref<{ must: ImpactItem[]; suggest: ImpactItem[]; total: number } | null>(null);

async function loadArchive(): Promise<void> {
  const body = await apiGet<{ inventory: { entities: ArchiveEntity[] } }>(
    `/projects/${currentProject()}/archive`,
  );
  entities.value = body.inventory.entities;
  const map: Record<string, string> = {};
  for (const e of entities.value) map[`entity:${e.id}`] = e.name;
  nameByRef.value = map;
}

onMounted(async () => {
  try {
    await loadArchive();
    if (entities.value.length) changes[0].target_ref = `entity:${entities.value[0].id}`;
  } catch (e) {
    notifyError(e);
  }
});

function addChange(): void {
  changes.push({
    change_type: "entity_delete",
    target_ref: entities.value.length ? `entity:${entities.value[0].id}` : "",
  });
}

function removeChange(index: number): void {
  changes.splice(index, 1);
}

function refLabel(ref: string): string {
  return nameByRef.value[ref] ? `${nameByRef.value[ref]} · ${ref}` : ref;
}

async function analyze(): Promise<void> {
  const payload = changes.filter((c) => c.target_ref.trim());
  if (!payload.length || running.value) return;
  running.value = true;
  result.value = null;
  try {
    const body = await apiPost<{ must_change: ImpactItem[]; suggest_check: ImpactItem[]; total: number }>(
      `/projects/${currentProject()}/impact:analyze`,
      { changes: payload, max_depth: maxDepth.value },
    );
    result.value = { must: body.must_change, suggest: body.suggest_check, total: body.total };
  } catch (e) {
    notifyError(e);
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="IMPACT" title="影响分析" purpose="预演一处改动会牵连到哪些内容。" />

    <div class="pane form">
      <div v-for="(row, index) in changes" :key="index" class="change-row">
        <select v-model="row.change_type">
          <option v-for="ct in CHANGE_TYPES" :key="ct.value" :value="ct.value">{{ ct.label }}</option>
        </select>
        <select v-if="entities.length" v-model="row.target_ref" class="target">
          <option v-for="e in entities" :key="e.id" :value="`entity:${e.id}`">
            {{ e.name }}（{{ e.type }}）
          </option>
        </select>
        <input v-else v-model="row.target_ref" class="target" placeholder="目标引用，如 entity:npc_x" />
        <button type="button" class="ghost" @click="removeChange(index)" :disabled="changes.length === 1">
          移除
        </button>
      </div>
      <div class="controls">
        <button type="button" class="ghost add" @click="addChange">+ 再加一处改动</button>
        <label class="depth">
          <span class="muted">推演深度 {{ maxDepth }}</span>
          <input v-model.number="maxDepth" type="range" min="1" max="4" />
        </label>
        <button class="primary" :disabled="running" @click="analyze">
          {{ running ? "推演中…" : "分析影响" }}
        </button>
      </div>
    </div>


    <div v-if="result" class="pane done">
      <div class="chips">
        <span class="chip red">必须同步改 <b>{{ result.must.length }}</b></span>
        <span class="chip amber">建议复查 <b>{{ result.suggest.length }}</b></span>
        <span class="chip">受影响合计 <b>{{ result.total }}</b></span>
      </div>
      <p v-if="!result.total" class="ok-text">没有发现下游影响——这处改动是安全的。</p>

      <template v-if="result.must.length">
        <div class="section sub"><span class="t red-t">必须同步修改</span></div>
        <TransitionGroup name="list" tag="div" class="items">
          <div v-for="(it, i) in result.must" :key="`m${i}`" class="item must">
            <b>{{ refLabel(it.target_ref) }}</b>
            <span class="muted reason">{{ it.reason }}</span>
            <span class="dist">距 {{ it.distance }}</span>
          </div>
        </TransitionGroup>
      </template>

      <template v-if="result.suggest.length">
        <div class="section sub"><span class="t">建议复查</span></div>
        <TransitionGroup name="list" tag="div" class="items">
          <div v-for="(it, i) in result.suggest" :key="`s${i}`" class="item">
            <b>{{ refLabel(it.target_ref) }}</b>
            <span class="muted reason">{{ it.reason }}</span>
            <span class="dist">距 {{ it.distance }}</span>
          </div>
        </TransitionGroup>
      </template>
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
  gap: 0.7rem;
}

.change-row {
  display: grid;
  grid-template-columns: 11rem 1fr auto;
  gap: 0.5rem;
}

.controls {
  display: flex;
  align-items: center;
  gap: 1rem;
  flex-wrap: wrap;
  margin-top: 0.3rem;
}

select,
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

select:focus,
input:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.depth {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.8rem;
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

button.ghost:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}

button.add {
  margin-right: auto;
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

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.5rem;
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

.ok-text {
  color: #8ed4ac;
}

.sub {
  margin-top: 0.6rem;
}

.red-t {
  color: #e89a9a !important;
}

.items {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}

.item {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 0.6rem;
  align-items: baseline;
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.5rem 0.75rem;
  font-size: 0.85rem;
}

.item.must {
  border-color: rgba(224, 133, 133, 0.35);
}

.item b {
  color: var(--ow-gold-bright);
}

.reason {
  font-size: 0.82rem;
}

.dist {
  font-size: 0.74rem;
  color: var(--ow-cyan);
  white-space: nowrap;
}

.list-enter-active {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
</style>
