<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { ApiError, apiGet, apiPost, currentProject, humanizeError } from "../api";
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
const entityTotal = ref(0);
const entityNextCursor = ref<string | null>(null);
const entityLoading = ref(false);
const entityError = ref("");
const ENTITY_PAGE_SIZE = 40;
let entityLoadSequence = 0;
const nameByRef = ref<Record<string, string>>({});
const changes = reactive<{ change_type: string; target_ref: string }[]>([
  { change_type: "entity_delete", target_ref: "" },
]);
const maxDepth = ref(2);
const running = ref(false);
const result = ref<{ must: ImpactItem[]; suggest: ImpactItem[]; total: number } | null>(null);
const RESULT_PAGE_SIZE = 50;
const visibleMustCount = ref(RESULT_PAGE_SIZE);
const visibleSuggestCount = ref(RESULT_PAGE_SIZE);
const visibleMust = computed(() => result.value?.must.slice(0, visibleMustCount.value) ?? []);
const visibleSuggest = computed(
  () => result.value?.suggest.slice(0, visibleSuggestCount.value) ?? [],
);

interface ArchiveEntityPage {
  items: ArchiveEntity[];
  total: number;
  next_cursor: string | null;
}

async function loadArchive(cursor = "", append = false): Promise<void> {
  if (append && entityLoading.value) return;
  const sequence = ++entityLoadSequence;
  entityLoading.value = true;
  if (!append) entityError.value = "";
  try {
    const params = new URLSearchParams({ kind: "entity", limit: String(ENTITY_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    const body = await apiGet<ArchiveEntityPage>(
      `/projects/${currentProject()}/archive?${params.toString()}`,
    );
    if (sequence !== entityLoadSequence) return;
    const rows = append ? [...entities.value, ...body.items] : body.items;
    entities.value = [...new Map(rows.map((entity) => [entity.id, entity])).values()];
    entityTotal.value = body.total;
    entityNextCursor.value = body.next_cursor;
    nameByRef.value = Object.fromEntries(
      entities.value.map((entity) => [`entity:${entity.id}`, entity.name]),
    );
    if (!changes[0].target_ref && entities.value.length) {
      changes[0].target_ref = `entity:${entities.value[0].id}`;
    }
  } catch (e) {
    if (sequence !== entityLoadSequence) return;
    if (cursor && e instanceof ApiError && e.status === 409) {
      await loadArchive();
      return;
    }
    entityError.value = humanizeError(e);
  } finally {
    if (sequence === entityLoadSequence) entityLoading.value = false;
  }
}

async function loadMoreEntities(): Promise<void> {
  if (!entityNextCursor.value) return;
  await loadArchive(entityNextCursor.value, true);
}

onMounted(async () => {
  try {
    await loadArchive();
  } catch (e) {
    notifyError(e);
  }
});

onUnmounted(() => {
  entityLoadSequence += 1;
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
  visibleMustCount.value = RESULT_PAGE_SIZE;
  visibleSuggestCount.value = RESULT_PAGE_SIZE;
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
      <div class="entity-pager">
        <span class="muted">目标选择器已载入 {{ entities.length }} / {{ entityTotal }} 个实体</span>
        <button
          v-if="entityNextCursor"
          type="button"
          class="ghost"
          :disabled="entityLoading"
          @click="loadMoreEntities"
        >
          {{ entityLoading ? "载入中…" : "加载下一页实体" }}
        </button>
      </div>
      <p v-if="entityError" class="error">读取实体失败：{{ entityError }}</p>
      <div v-for="(row, index) in changes" :key="index" class="change-row">
        <select v-model="row.change_type">
          <option v-for="ct in CHANGE_TYPES" :key="ct.value" :value="ct.value">{{ ct.label }}</option>
        </select>
        <input
          v-model="row.target_ref"
          class="target"
          list="impact-entity-refs"
          placeholder="任意对象引用，如 entity:npc_x、quest:q_1、relation:a:kind:b"
        />
        <button type="button" class="ghost" @click="removeChange(index)" :disabled="changes.length === 1">
          移除
        </button>
      </div>
      <datalist id="impact-entity-refs">
        <option v-for="e in entities" :key="e.id" :value="`entity:${e.id}`">
          {{ e.name }}（{{ e.type }}）
        </option>
      </datalist>
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
          <div v-for="(it, i) in visibleMust" :key="`m${i}`" class="item must">
            <b>{{ refLabel(it.target_ref) }}</b>
            <span class="muted reason">{{ it.reason }}</span>
            <span class="dist">距 {{ it.distance }}</span>
          </div>
        </TransitionGroup>
        <button
          v-if="visibleMustCount < result.must.length"
          type="button"
          class="ghost result-more"
          @click="visibleMustCount += RESULT_PAGE_SIZE"
        >显示更多必须修改项（剩余 {{ result.must.length - visibleMustCount }}）</button>
      </template>

      <template v-if="result.suggest.length">
        <div class="section sub"><span class="t">建议复查</span></div>
        <TransitionGroup name="list" tag="div" class="items">
          <div v-for="(it, i) in visibleSuggest" :key="`s${i}`" class="item">
            <b>{{ refLabel(it.target_ref) }}</b>
            <span class="muted reason">{{ it.reason }}</span>
            <span class="dist">距 {{ it.distance }}</span>
          </div>
        </TransitionGroup>
        <button
          v-if="visibleSuggestCount < result.suggest.length"
          type="button"
          class="ghost result-more"
          @click="visibleSuggestCount += RESULT_PAGE_SIZE"
        >显示更多建议复查项（剩余 {{ result.suggest.length - visibleSuggestCount }}）</button>
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

.entity-pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
  font-size: 0.78rem;
}

.error {
  color: #e89a9a;
  font-size: 0.8rem;
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
  border-radius: var(--ow-control-radius);
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
  border-radius: var(--ow-control-radius);
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

@media (max-width: 640px) {
  .form { padding: 0.85rem; }
  .change-row { grid-template-columns: 1fr; }
  .change-row > * { min-width: 0; width: 100%; }
  .controls { align-items: stretch; flex-direction: column; gap: 0.6rem; }
  .controls button { width: 100%; min-height: 44px; }
  .item { grid-template-columns: 1fr auto; }
  .item .reason { grid-column: 1 / -1; }
}
</style>
