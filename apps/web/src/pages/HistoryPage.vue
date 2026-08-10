<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ApiError, humanizeError, apiGet, apiPost, currentProject } from "../api";
import PageHead from "../components/PageHead.vue";
import AsyncState from "../components/AsyncState.vue";

// 变更史 = 给世界拍快照，再把任意快照与当前世界做结构化对比（只读、$0；正典本就是可 diff 的文件）。
interface SnapshotMeta {
  id: string;
  label: string;
  created_at: string;
  content_hash: string;
}
interface FieldChange {
  field: string;
  before: unknown;
  after: unknown;
}
interface ObjectChange {
  kind: string;
  id: string;
  name: string;
  changes: FieldChange[];
}
interface CanonDiff {
  from_id: string;
  added: ObjectChange[];
  removed: ObjectChange[];
  changed: ObjectChange[];
  summary: Record<string, number>;
}

const KIND_LABEL: Record<string, string> = {
  entity: "实体",
  quest: "任务",
  region: "区域",
  poi: "地点",
  dialogue: "对话",
  dialogue_tree: "对话树",
  term: "术语",
  localized_text: "本地化",
  quest_event_ref: "事件引用",
  relation: "关系",
};

const snapshots = ref<SnapshotMeta[]>([]);
const snapshotTotal = ref(0);
const nextSnapshotCursor = ref<string | null>(null);
const selected = ref("");
const diff = ref<CanonDiff | null>(null);
const label = ref("");
const error = ref("");
const busy = ref(false);
const listLoading = ref(true);
const listLoadingMore = ref(false);
const listError = ref("");
const listMoreError = ref("");
const diffLoading = ref(false);
const diffError = ref("");
let snapshotLoadEpoch = 0;

function fmtTime(iso: string): string {
  return iso.slice(0, 19).replace("T", " ");
}

function fmtVal(v: unknown): string {
  if (v === null || v === undefined || v === "") return "（空）";
  const s = typeof v === "string" ? v : JSON.stringify(v);
  return s.length > 80 ? s.slice(0, 80) + "…" : s;
}

async function loadSnapshots(
  cursor: string | null = null,
  append = false,
  epoch = ++snapshotLoadEpoch,
): Promise<void> {
  const params = new URLSearchParams({ limit: "30" });
  if (cursor) params.set("cursor", cursor);
  const body = await apiGet<{
    snapshots: SnapshotMeta[];
    total?: number;
    next_cursor?: string | null;
  }>(
    `/projects/${currentProject()}/snapshots?${params.toString()}`,
  );
  if (epoch !== snapshotLoadEpoch) return;
  const merged = append ? [...snapshots.value, ...body.snapshots] : body.snapshots;
  snapshots.value = merged;
  snapshotTotal.value = body.total ?? merged.length;
  nextSnapshotCursor.value = body.next_cursor ?? null;
  if (!append) listMoreError.value = "";
}

async function loadSnapshotList(): Promise<void> {
  const epoch = ++snapshotLoadEpoch;
  listLoading.value = true;
  listError.value = "";
  try {
    await loadSnapshots(null, false, epoch);
  } catch (e) {
    listError.value = humanizeError(e);
  } finally {
    listLoading.value = false;
  }
}

async function loadMoreSnapshots(): Promise<void> {
  const cursor = nextSnapshotCursor.value;
  if (!cursor || listLoadingMore.value) return;
  const epoch = snapshotLoadEpoch;
  listLoadingMore.value = true;
  listMoreError.value = "";
  try {
    await loadSnapshots(cursor, true, epoch);
  } catch (e) {
    if (epoch === snapshotLoadEpoch && e instanceof ApiError && [409, 422].includes(e.status)) {
      listLoadingMore.value = false;
      await loadSnapshotList();
      return;
    }
    if (epoch === snapshotLoadEpoch) listMoreError.value = humanizeError(e);
  } finally {
    if (epoch === snapshotLoadEpoch) listLoadingMore.value = false;
  }
}

async function takeSnapshot(): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  error.value = "";
  try {
    await apiPost(`/projects/${currentProject()}/snapshots`, { label: label.value.trim() });
    label.value = "";
    await loadSnapshots();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

async function restoreSnapshot(id: string): Promise<void> {
  if (busy.value) return;
  if (!confirm("恢复到此快照？当前世界会被切回该分支状态（建议先给当前状态拍一张快照）。")) return;
  busy.value = true;
  error.value = "";
  try {
    await apiPost(`/projects/${currentProject()}/snapshots:restore`, { snapshot_id: id });
    await loadSnapshots();
    diff.value = null;
    selected.value = "";
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

async function openDiff(id: string): Promise<void> {
  selected.value = id;
  diff.value = null;
  diffError.value = "";
  diffLoading.value = true;
  try {
    const body = await apiGet<{ diff: CanonDiff }>(
      `/projects/${currentProject()}/diff?from=${encodeURIComponent(id)}`,
    );
    if (selected.value === id) diff.value = body.diff;
  } catch (e) {
    if (selected.value === id) diffError.value = humanizeError(e);
  } finally {
    if (selected.value === id) diffLoading.value = false;
  }
}

onMounted(loadSnapshotList);
</script>

<template>
  <section>
    <PageHead
      overline="HISTORY"
      title="变更史 · 分支"
      purpose="快照即检查点/分支：存档、对比增删改、随时恢复到任一快照做 what-if 变体。"
    />

    <div class="pane take">
      <input v-model="label" maxlength="120" placeholder="给这个快照起个名（可留空，如：上线前 v1）" />
      <button class="primary" :disabled="busy" @click="takeSnapshot">
        {{ busy ? "存档中…" : "拍快照" }}
      </button>
    </div>

    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <AsyncState
      v-if="listLoading"
      state="loading"
      title="正在读取变更史"
      hint="快照和分支记录会显示在这里。"
      compact
    />
    <AsyncState
      v-else-if="listError"
      state="error"
      title="变更史读取失败"
      :hint="listError"
      action-label="重新读取"
      compact
      @action="loadSnapshotList"
    />
    <AsyncState
      v-else-if="!snapshots.length"
      state="empty"
      title="还没有快照"
      hint="先为当前世界拍一个快照；之后每次重要改动都可以回来对比或恢复。"
      compact
    />

    <div v-else class="layout">
      <ul class="list">
        <li
          v-for="s in snapshots"
          :key="s.id"
          class="pane row"
          :class="{ on: selected === s.id }"
          role="button"
          tabindex="0"
          :aria-pressed="selected === s.id"
          @click="openDiff(s.id)"
          @keydown.enter="openDiff(s.id)"
          @keydown.space.prevent="openDiff(s.id)"
        >
          <b>{{ s.label || "未命名快照" }}</b>
          <span class="meta muted">{{ fmtTime(s.created_at) }}</span>
        </li>
        <li class="pager" aria-label="快照分页">
          <span class="muted meta">已显示 {{ snapshots.length }} / {{ snapshotTotal }}</span>
          <button
            v-if="nextSnapshotCursor"
            class="page-button"
            :disabled="listLoadingMore"
            @click="loadMoreSnapshots"
          >
            {{ listLoadingMore ? "加载中…" : "加载下一页" }}
          </button>
          <span v-else class="muted meta">已到末页</span>
          <span v-if="listMoreError" class="error meta" role="alert">{{ listMoreError }}</span>
        </li>
      </ul>

      <div class="stage">
        <AsyncState
          v-if="diffLoading"
          state="loading"
          title="正在计算差异"
          hint="将所选快照与当前世界逐项比较。"
          compact
        />
        <AsyncState
          v-else-if="diffError"
          state="error"
          title="差异读取失败"
          :hint="diffError"
          action-label="重新比较"
          compact
          @action="openDiff(selected)"
        />
        <div v-else-if="diff" class="diff">
          <div class="sum">
            与当前世界相比：
            <span class="chip add">新增 {{ diff.summary.added }}</span>
            <span class="chip del">删除 {{ diff.summary.removed }}</span>
            <span class="chip chg">修改 {{ diff.summary.changed }}</span>
            <button class="restore" :disabled="busy" @click="restoreSnapshot(selected)">
              恢复到此快照
            </button>
          </div>

          <p
            v-if="!diff.summary.added && !diff.summary.removed && !diff.summary.changed"
            class="muted"
          >
            这个快照和当前世界完全一致——没有任何改动。
          </p>

          <template v-if="diff.added.length">
            <div class="section sub"><span class="t">新增</span></div>
            <div class="rows">
              <div v-for="o in diff.added" :key="o.kind + o.id" class="ch add">
                <span class="k">{{ KIND_LABEL[o.kind] ?? o.kind }}</span>
                <span class="nm">{{ o.name || o.id }}</span>
              </div>
            </div>
          </template>

          <template v-if="diff.removed.length">
            <div class="section sub"><span class="t">删除</span></div>
            <div class="rows">
              <div v-for="o in diff.removed" :key="o.kind + o.id" class="ch del">
                <span class="k">{{ KIND_LABEL[o.kind] ?? o.kind }}</span>
                <span class="nm">{{ o.name || o.id }}</span>
              </div>
            </div>
          </template>

          <template v-if="diff.changed.length">
            <div class="section sub"><span class="t">修改</span></div>
            <div class="rows">
              <div v-for="o in diff.changed" :key="o.kind + o.id" class="ch chg col">
                <div class="ch-head">
                  <span class="k">{{ KIND_LABEL[o.kind] ?? o.kind }}</span>
                  <span class="nm">{{ o.name || o.id }}</span>
                </div>
                <div v-for="f in o.changes" :key="f.field" class="field">
                  <span class="fn">{{ f.field }}</span>
                  <span class="before">{{ fmtVal(f.before) }}</span>
                  <span class="arrow">→</span>
                  <span class="after">{{ fmtVal(f.after) }}</span>
                </div>
              </div>
            </div>
          </template>
        </div>
        <p v-else class="muted pick">选择左侧一个快照，查看它与当前世界的差异。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.take {
  display: flex;
  gap: 0.6rem;
  padding: 0.8rem 1rem;
  align-items: center;
}

.take input {
  flex: 1;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.86rem;
}

.take input:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
  color: #241a05;
  font-weight: 600;
  padding: 0.5rem 1rem;
  cursor: pointer;
  white-space: nowrap;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.empty {
  margin-top: 0.8rem;
}

.layout {
  display: grid;
  grid-template-columns: 15rem 1fr;
  gap: 0.9rem;
  align-items: start;
  margin-top: 0.8rem;
}

.list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.row {
  padding: 0.55rem 0.8rem;
  cursor: pointer;
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  border-left: 2px solid transparent;
}

.row.on {
  border-left-color: var(--ow-gold);
  background: var(--ow-gold-faint);
}

.row b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
  font-size: 0.9rem;
}

.meta {
  font-size: 0.74rem;
  font-family: ui-monospace, Consolas, monospace;
}

.stage {
  min-height: 10rem;
}

.pick {
  padding: 2rem 0;
  text-align: center;
}

.sum {
  font-size: 0.86rem;
  color: var(--ow-muted);
  display: flex;
  align-items: center;
  gap: 0.4rem;
  flex-wrap: wrap;
  margin-bottom: 0.6rem;
}

.chip {
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  font-size: 0.76rem;
  padding: 0.1rem 0.55rem;
  border: 1px solid var(--ow-line);
}

.restore {
  margin-left: auto;
  background: transparent;
  border: 1px solid var(--ow-gold, #d8b46a);
  color: var(--ow-gold, #d8b46a);
  border-radius: 0.4rem;
  padding: 0.2rem 0.6rem;
  font-size: 0.76rem;
  cursor: pointer;
}
.restore:disabled {
  opacity: 0.5;
}
.chip.add {
  color: #8ed4ac;
  border-color: rgba(142, 212, 172, 0.4);
}

.chip.del {
  color: #e89a9a;
  border-color: rgba(224, 133, 133, 0.4);
}

.chip.chg {
  color: #e0a878;
  border-color: rgba(224, 168, 120, 0.4);
}

.sub {
  margin: 0.7rem 0 0.3rem;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.ch {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  padding: 0.35rem 0.6rem;
  border-radius: var(--ow-control-radius);
  border-left: 2px solid transparent;
  background: rgba(16, 22, 48, 0.5);
  font-size: 0.84rem;
}

.ch.col {
  flex-direction: column;
  align-items: stretch;
  gap: 0.25rem;
}

.ch.add {
  border-left-color: #8ed4ac;
}

.ch.del {
  border-left-color: #e89a9a;
}

.ch.chg {
  border-left-color: #e0a878;
}

.ch-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
}

.k {
  color: var(--ow-muted);
  font-size: 0.74rem;
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.02rem 0.4rem;
}

.nm {
  color: var(--ow-ink);
  font-family: var(--ow-serif);
}

.field {
  display: flex;
  align-items: baseline;
  gap: 0.4rem;
  font-size: 0.8rem;
  padding-left: 0.3rem;
}

.fn {
  color: var(--ow-cyan);
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  min-width: 5rem;
}

.before {
  color: #e0a3a3;
  text-decoration: line-through;
  opacity: 0.8;
}

.arrow {
  color: var(--ow-muted);
}

.after {
  color: #9fd6b4;
}

.error {
  color: #e89a9a;
}

@media (max-width: 720px) {
  .layout {
    grid-template-columns: 1fr;
  }

  .take {
    align-items: stretch;
    flex-direction: column;
  }

  .take input,
  .take button {
    min-height: 44px;
    width: 100%;
  }

  .list {
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(min(100%, 12rem), 1fr));
  }

  .field {
    align-items: stretch;
    flex-direction: column;
    gap: 0.2rem;
  }

  .fn {
    min-width: 0;
  }

  .arrow {
    transform: rotate(90deg);
    align-self: flex-start;
  }
}

.pager {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  align-items: stretch;
}

.page-button {
  min-height: 38px;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  cursor: pointer;
  font: inherit;
  font-size: 0.78rem;
}

.page-button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
</style>
