<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from "vue";
import PageHead from "../components/PageHead.vue";
import Modal from "../components/Modal.vue";
import {
  ApiError,
  currentTeamBearer,
  humanizeError,
  setTeamBearer,
  teamApiGet,
} from "../api";

type CanonicalHead = "authoring" | "serving";

interface CanonicalObject {
  object_version_id: string;
  logical_id: string;
  object_type: string;
  payload_json?: string;
  payload_hash?: string;
  [key: string]: unknown;
}

interface CanonicalMembership {
  logical_id: string;
  object_version_id?: string | null;
  tombstone: boolean;
}

interface CanonicalState {
  selected_head: CanonicalHead;
  revision: {
    revision_id: string;
    parent_revision?: string | null;
    manifest_hash: string;
    created_at: string;
    created_by: string;
  };
  objects: CanonicalObject[];
  memberships: CanonicalMembership[];
  object_types?: string[];
  pagination: {
    collection: "objects" | "memberships";
    total: number;
    limit: number;
    offset: number;
    page_cursor?: string | null;
    previous_cursor?: string | null;
    next_cursor?: string | null;
    snapshot_etag: string;
  };
  summary: {
    object_count: number;
    membership_count: number;
    tombstone_count: number;
    relation_set_count: number;
    source_ref_count: number;
  };
  authorization?: { subject_id?: string; membership_role?: string | null };
  release?: { generation?: number; pointer?: Record<string, unknown> };
}

const containerId = ref(localStorage.getItem("owcopilot_team_container") ?? "");
const bearer = ref(currentTeamBearer());
const rememberBearer = ref(Boolean(localStorage.getItem("owcopilot_team_bearer")));
const head = ref<CanonicalHead>("authoring");
const state = ref<CanonicalState | null>(null);
const loading = ref(false);
const downloading = ref(false);
const error = ref("");
const notice = ref("");
const query = ref("");
const typeFilter = ref("");
const selected = ref<CanonicalObject | null>(null);
const PAGE_SIZE = 50;
let requestSerial = 0;
let filterTimer = 0;
const payloadCache = new WeakMap<CanonicalObject, Record<string, unknown> | null>();
const titleCache = new WeakMap<CanonicalObject, string>();

const objectTypes = computed(() => state.value?.object_types ?? []);
const pageCount = computed(() =>
  Math.max(1, Math.ceil((state.value?.pagination.total ?? 0) / PAGE_SIZE)),
);
const currentPage = computed(() =>
  Math.floor((state.value?.pagination.offset ?? 0) / PAGE_SIZE) + 1,
);

function parsedPayload(item: CanonicalObject): Record<string, unknown> | null {
  if (payloadCache.has(item)) return payloadCache.get(item) ?? null;
  if (!item.payload_json) return null;
  try {
    const result = JSON.parse(item.payload_json) as unknown;
    const payload = result && typeof result === "object" && !Array.isArray(result)
      ? result as Record<string, unknown>
      : null;
    payloadCache.set(item, payload);
    return payload;
  } catch {
    payloadCache.set(item, null);
    return null;
  }
}
function payloadTitle(item: CanonicalObject): string {
  const cached = titleCache.get(item);
  if (cached) return cached;
  const payload = parsedPayload(item);
  for (const key of ["name", "title", "label", "text"]) {
    const value = payload?.[key];
    if (typeof value === "string" && value.trim()) {
      titleCache.set(item, value.trim());
      return value.trim();
    }
  }
  titleCache.set(item, item.logical_id);
  return item.logical_id;
}
function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}
function short(value: string, size = 12): string {
  return value.length > size ? `${value.slice(0, size)}…` : value;
}

function canonicalPath(
  collection: "objects" | "memberships",
  cursor: string | null,
  withFilters: boolean,
  limit = PAGE_SIZE,
): string {
  const params = new URLSearchParams({ head: head.value, collection, limit: String(limit) });
  if (cursor) params.set("cursor", cursor);
  if (withFilters && query.value.trim()) params.set("q", query.value.trim());
  if (withFilters && typeFilter.value) params.set("object_type", typeFilter.value);
  return `/team/containers/${encodeURIComponent(containerId.value.trim())}/canonical?${params.toString()}`;
}

async function load(cursor: string | null = null): Promise<void> {
  if (!containerId.value.trim() || !bearer.value.trim()) return;
  const sequence = ++requestSerial;
  loading.value = true;
  error.value = "";
  notice.value = "";
  setTeamBearer(bearer.value, rememberBearer.value);
  localStorage.setItem("owcopilot_team_container", containerId.value.trim());
  try {
    const next = await teamApiGet<CanonicalState>(canonicalPath("objects", cursor, true));
    if (sequence !== requestSerial) return;
    state.value = next;
    selected.value = null;
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 409 && cursor) {
      try {
        const refreshed = await teamApiGet<CanonicalState>(canonicalPath("objects", null, true));
        if (sequence !== requestSerial) return;
        state.value = refreshed;
        selected.value = null;
        notice.value = "列表在翻页期间发生变化，已安全回到最新第一页。";
      } catch (refreshError) {
        if (sequence !== requestSerial) return;
        state.value = null;
        error.value = humanizeError(refreshError);
      }
    } else if (sequence === requestSerial) {
      state.value = null;
      error.value = humanizeError(caught);
    }
  } finally {
    if (sequence === requestSerial) loading.value = false;
  }
}
async function switchHead(value: CanonicalHead): Promise<void> {
  if (head.value === value) return;
  head.value = value;
  query.value = "";
  typeFilter.value = "";
  if (state.value) await load(null);
}

async function downloadSnapshot(): Promise<void> {
  if (!state.value || downloading.value) return;
  downloading.value = true;
  error.value = "";
  const expectedRevision = state.value.revision.revision_id;
  try {
    let exportState: CanonicalState | null = null;
    const objects: CanonicalObject[] = [];
    const memberships: CanonicalMembership[] = [];
    for (const collection of ["objects", "memberships"] as const) {
      let cursor: string | null = null;
      do {
        const response: CanonicalState = await teamApiGet<CanonicalState>(
          canonicalPath(collection, cursor, false, 100),
        );
        if (response.revision.revision_id !== expectedRevision) {
          throw new ApiError(409, "canonical head changed during snapshot export", "STALE_CURSOR", "team");
        }
        exportState ??= response;
        if (collection === "objects") objects.push(...response.objects);
        else memberships.push(...response.memberships);
        cursor = response.pagination.next_cursor ?? null;
      } while (cursor);
    }
    if (!exportState) return;
    const snapshot = { ...exportState, objects, memberships, pagination: undefined };
    const blob = new Blob([JSON.stringify(snapshot, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `canonical-${head.value}-${expectedRevision}.json`;
    anchor.click();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    downloading.value = false;
  }
}

watch([query, typeFilter], () => {
  window.clearTimeout(filterTimer);
  if (!state.value) return;
  filterTimer = window.setTimeout(() => void load(null), 250);
});

onMounted(() => {
  if (containerId.value && bearer.value) void load();
});
onUnmounted(() => {
  requestSerial += 1;
  window.clearTimeout(filterTimer);
});
</script>

<template>
  <section>
    <PageHead
      overline="TEAM CANON"
      title="团队正典"
      purpose="查看当前编纂头与发布头的不可变修订、对象和来源链。"
    >
      <template #aside>
        <span v-if="state" class="head-state">{{ state.selected_head === "authoring" ? "编纂中" : "已发布" }}</span>
      </template>
    </PageHead>

    <div class="pane connection-pane">
      <div class="connection-grid">
        <label>
          <span>容器 ID</span>
          <input v-model="containerId" autocomplete="off" placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" />
        </label>
        <label>
          <span>团队访问令牌</span>
          <input v-model="bearer" type="password" autocomplete="current-password" placeholder="Bearer token" />
          <span class="token-memory"><input v-model="rememberBearer" type="checkbox" /> 在这台设备长期记住</span>
        </label>
        <button class="primary" :disabled="loading || !containerId.trim() || !bearer.trim()" @click="load(null)">
          {{ loading ? "正在读取…" : "读取正典" }}
        </button>
      </div>
      <p class="connection-hint">令牌默认只保留到当前浏览器会话结束；容器与来源摄取页共用。</p>
    </div>

    <div class="head-tabs" role="tablist" aria-label="正典头">
      <button
        v-for="option in (['authoring', 'serving'] as CanonicalHead[])"
        :key="option"
        role="tab"
        :aria-selected="head === option"
        :class="{ on: head === option }"
        @click="switchHead(option)"
      >
        <i aria-hidden="true"></i>{{ option === "authoring" ? "编纂头" : "发布头" }}
      </button>
    </div>

    <p v-if="notice" class="pane recovery-notice" role="status">{{ notice }}</p>

    <div v-if="error" class="pane async-state error-state" role="alert">
      <span class="state-sigil">!</span>
      <div><b>没有读到正典</b><p>{{ error }}</p></div>
      <button @click="load(null)">重试</button>
    </div>
    <div v-else-if="loading" class="loading-grid" role="status" aria-label="正在读取团队正典">
      <div v-for="item in 5" :key="item" class="pane skeleton-card"><span></span><i></i></div>
    </div>
    <div v-else-if="!state" class="pane async-state empty-state">
      <span class="state-sigil">✦</span>
      <div><b>输入团队连接信息</b><p>读取后可在这里核对修订、对象总量、逻辑删除与来源引用。</p></div>
    </div>

    <template v-else>
      <div class="summary-grid">
        <div class="pane summary"><span>正典对象</span><b class="num">{{ state.summary.object_count }}</b></div>
        <div class="pane summary"><span>清单成员</span><b class="num">{{ state.summary.membership_count }}</b></div>
        <div class="pane summary"><span>逻辑删除</span><b class="num">{{ state.summary.tombstone_count }}</b></div>
        <div class="pane summary"><span>关系集合</span><b class="num">{{ state.summary.relation_set_count }}</b></div>
        <div class="pane summary"><span>来源引用</span><b class="num">{{ state.summary.source_ref_count }}</b></div>
      </div>

      <div class="pane revision-card">
        <div class="revision-title"><span class="overline">REVISION</span><b>{{ short(state.revision.revision_id, 18) }}</b></div>
        <dl>
          <div><dt>创建时间</dt><dd>{{ formatDate(state.revision.created_at) }}</dd></div>
          <div><dt>创建者</dt><dd class="mono">{{ short(state.revision.created_by, 18) }}</dd></div>
          <div><dt>清单摘要</dt><dd class="mono" :title="state.revision.manifest_hash">{{ short(state.revision.manifest_hash, 20) }}</dd></div>
          <div><dt>成员角色</dt><dd>{{ state.authorization?.membership_role ?? "—" }}</dd></div>
        </dl>
      </div>

      <div class="section"><span class="t">对象清单</span></div>
      <div class="object-tools">
        <label class="search-field"><span class="sr-only">搜索对象</span><input v-model="query" placeholder="搜索名称、逻辑 ID 或类型…" /></label>
        <select v-model="typeFilter" aria-label="按对象类型筛选">
          <option value="">全部类型</option>
          <option v-for="kind in objectTypes" :key="kind" :value="kind">{{ kind }}</option>
        </select>
        <span class="result-count">本页 {{ state.objects.length }} / 共 {{ state.pagination.total }}</span>
        <button class="export-button" :disabled="downloading" @click="downloadSnapshot">
          {{ downloading ? "正在汇总…" : "下载完整快照" }}
        </button>
      </div>

      <div v-if="state.objects.length" class="object-list">
        <button v-for="item in state.objects" :key="item.object_version_id" class="pane object-row" @click="selected = item">
          <span class="object-kind">{{ item.object_type }}</span>
          <span class="object-name">{{ payloadTitle(item) }}</span>
          <span class="object-id mono">{{ item.logical_id }}</span>
          <span class="object-open" aria-hidden="true">›</span>
        </button>
      </div>
      <div v-else class="pane no-results">没有符合当前筛选条件的对象。</div>
      <nav v-if="pageCount > 1" class="pagination" aria-label="对象分页">
        <button
          :disabled="loading || !state.pagination.previous_cursor"
          @click="load(state.pagination.previous_cursor ?? null)"
        >上一页</button>
        <span>第 {{ currentPage }} / {{ pageCount }} 页 · 服务端每页最多 {{ PAGE_SIZE }} 条</span>
        <button
          :disabled="loading || !state.pagination.next_cursor"
          @click="load(state.pagination.next_cursor ?? null)"
        >下一页</button>
      </nav>
    </template>

    <Modal :open="Boolean(selected)" overline="CANONICAL OBJECT" :title="selected ? payloadTitle(selected) : ''" wide @close="selected = null">
      <div v-if="selected" class="object-detail">
        <dl>
          <div><dt>逻辑 ID</dt><dd class="mono">{{ selected.logical_id }}</dd></div>
          <div><dt>版本 ID</dt><dd class="mono">{{ selected.object_version_id }}</dd></div>
          <div><dt>对象类型</dt><dd>{{ selected.object_type }}</dd></div>
        </dl>
        <pre>{{ JSON.stringify(parsedPayload(selected) ?? selected, null, 2) }}</pre>
      </div>
    </Modal>
  </section>
</template>

<style scoped>
.connection-pane { padding: 1rem 1.1rem; }
.connection-grid { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1fr) auto; gap: 0.75rem; align-items: end; }
.connection-grid label { min-width: 0; }
.connection-grid label span { display: block; margin-bottom: 0.35rem; color: var(--ow-ink-soft); font-size: 0.76rem; }
.connection-grid .token-memory { display: flex; align-items: center; gap: .35rem; margin: .4rem 0 0; color: var(--ow-muted); font-size: .72rem; }
.token-memory input { width: 1rem; height: 1rem; accent-color: var(--ow-gold-bright); }
.connection-grid input,
.object-tools input,
.object-tools select {
  width: 100%; padding: 0.58rem 0.7rem; border: 1px solid var(--ow-line);
  color: var(--ow-ink); background: var(--ow-panel-2); font: inherit;
}
.connection-grid button,
.async-state button,
.export-button {
  min-height: 39px; padding: 0.55rem 0.9rem; border: 1px solid var(--ow-gold-soft);
  color: var(--ow-gold-bright); background: var(--ow-gold-faint); cursor: pointer;
}
.connection-grid button:disabled { opacity: 0.5; cursor: not-allowed; }
.connection-hint { margin: 0.55rem 0 0; color: var(--ow-muted); font-size: 0.74rem; }
.head-state { padding: 0.25rem 0.55rem; border: 1px solid var(--ow-gold-soft); color: var(--ow-gold-bright); font-size: 0.74rem; }
.head-tabs { display: flex; gap: 0.35rem; margin: 0.8rem 0; border-bottom: 1px solid var(--ow-line); }
.head-tabs button { min-height: 42px; padding: 0.55rem 0.9rem; border: 0; border-bottom: 2px solid transparent; color: var(--ow-muted); background: transparent; cursor: pointer; }
.head-tabs button.on { border-bottom-color: var(--ow-gold-bright); color: var(--ow-gold-bright); }
.head-tabs i { display: inline-block; width: 6px; height: 6px; margin-right: 0.45rem; border: 1px solid currentColor; transform: rotate(45deg); }
.head-tabs .on i { background: currentColor; }
.async-state { min-height: 180px; padding: 2rem; display: flex; align-items: center; justify-content: center; gap: 1rem; text-align: left; }
.async-state b { color: var(--ow-ink); }
.async-state p { margin: 0.3rem 0 0; color: var(--ow-muted); line-height: 1.6; }
.state-sigil { width: 42px; height: 42px; display: grid; place-items: center; border: 1px solid var(--ow-gold-soft); color: var(--ow-gold-bright); transform: rotate(45deg); }
.error-state { border-color: var(--ow-cinnabar-soft); }
.recovery-notice { margin: 0 0 .8rem; padding: .7rem .9rem; border-color: var(--ow-gold-soft); color: var(--ow-gold-bright); font-size: .78rem; }
.error-state .state-sigil { color: #efae8e; border-color: var(--ow-cinnabar-soft); }
.loading-grid,
.summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 0.7rem; }
.skeleton-card { height: 92px; padding: 1rem; }
.skeleton-card span,
.skeleton-card i { display: block; height: 10px; margin-bottom: 0.8rem; background: linear-gradient(90deg, var(--ow-line), var(--ow-gold-faint), var(--ow-line)); background-size: 200% 100%; animation: shine 1.2s linear infinite; }
.skeleton-card i { width: 45%; height: 22px; }
@keyframes shine { to { background-position: -200% 0; } }
.summary { padding: 0.9rem 1rem; }
.summary span { display: block; color: var(--ow-muted); font-size: 0.74rem; }
.summary b { display: block; margin-top: 0.25rem; color: var(--ow-gold-bright); font-size: 1.7rem; font-weight: 500; }
.revision-card { margin-top: 0.8rem; padding: 1rem 1.1rem; display: flex; gap: 1.5rem; align-items: center; }
.revision-title { min-width: 170px; display: flex; flex-direction: column; gap: 0.25rem; }
.revision-card dl,
.object-detail dl { flex: 1; margin: 0; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 0.65rem 1rem; }
.revision-card dl div,
.object-detail dl div { min-width: 0; }
dt { color: var(--ow-muted); font-size: 0.7rem; } dd { margin: 0.15rem 0 0; color: var(--ow-ink-soft); overflow-wrap: anywhere; font-size: 0.8rem; }
.object-tools { display: grid; grid-template-columns: minmax(180px, 1fr) 180px auto auto; gap: 0.55rem; align-items: center; margin-bottom: 0.7rem; }
.result-count { color: var(--ow-muted); font-size: 0.76rem; white-space: nowrap; }
.object-list { display: flex; flex-direction: column; gap: 0.45rem; }
.object-row {
  width: 100%; display: grid; grid-template-columns: 9rem minmax(0, 1fr) minmax(10rem, 0.8fr) auto;
  gap: 0.8rem; align-items: center; padding: 0.75rem 0.9rem; color: var(--ow-ink); text-align: left; cursor: pointer;
}
.object-kind { color: var(--ow-violet); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.06em; }
.object-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.object-id { overflow: hidden; color: var(--ow-muted); font-size: 0.72rem; text-overflow: ellipsis; white-space: nowrap; }
.object-open { color: var(--ow-gold-bright); font-size: 1.3rem; }
.no-results { padding: 2rem; color: var(--ow-muted); text-align: center; }
.pagination { display: flex; align-items: center; justify-content: center; gap: .7rem; margin-top: .8rem; background: none; }
.pagination span { color: var(--ow-muted); font-size: .74rem; }
.pagination button { min-height: 38px; padding: .45rem .8rem; border: 1px solid var(--ow-line); color: var(--ow-ink-soft); background: var(--ow-panel-2); cursor: pointer; }
.pagination button:disabled { opacity: .4; cursor: not-allowed; }
.object-detail pre { max-height: 55vh; overflow: auto; padding: 0.9rem; border: 1px solid var(--ow-line); color: var(--ow-ink-soft); background: rgba(5, 8, 22, 0.5); font-size: 0.76rem; line-height: 1.55; white-space: pre-wrap; overflow-wrap: anywhere; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }
@media (max-width: 760px) {
  .connection-grid,
  .object-tools { grid-template-columns: 1fr; }
  .connection-grid button,
  .export-button { min-height: 44px; }
  .revision-card { align-items: stretch; flex-direction: column; }
  .revision-card dl,
  .object-detail dl { grid-template-columns: 1fr; }
  .object-row { grid-template-columns: 1fr auto; gap: 0.3rem 0.6rem; min-height: 76px; }
  .object-kind,
  .object-id { grid-column: 1; }
  .object-open { grid-column: 2; grid-row: 1 / 4; }
  .async-state { padding: 1.3rem 1rem; flex-wrap: wrap; }
  .pagination { justify-content: space-between; }
  .pagination button { min-height: 44px; }
}
</style>
