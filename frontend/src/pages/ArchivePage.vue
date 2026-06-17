<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { apiDelete, apiGet, apiPatch, apiPost, currentProject } from "../api";
import { notifyError, notifyOk } from "../toast";
import PageHead from "../components/PageHead.vue";
import Modal from "../components/Modal.vue";

interface EntityRow {
  id: string;
  name: string;
  type: string;
  description: string;
  tags: string;
  origin: string;
  review_status: string;
  metadata?: Record<string, unknown>;
}

const entities = ref<EntityRow[]>([]);
const query = ref("");
const typeFilter = ref("");
const sortKey = ref<"name" | "type" | "id">("name");

const TYPE_LABELS: Record<string, string> = {
  npc: "角色",
  location: "地点",
  faction: "势力",
  item: "物品",
  region: "区域",
  term: "术语",
  skill: "技能",
  achievement: "成就",
  concept: "概念",
  event: "事件",
};
const PROFILE_LABELS: Record<string, string> = {
  appearance: "外貌",
  personality: "性格",
  backstory: "背景故事",
  motivation: "动机与目标",
  abilities: "能力与专长",
  weakness: "弱点与恐惧",
  voice: "说话方式",
};
const typeLabel = (t: string): string => TYPE_LABELS[t] ?? t;

const presentTypes = computed(() =>
  [...new Set(entities.value.map((e) => e.type))].sort((a, b) =>
    typeLabel(a).localeCompare(typeLabel(b), "zh"),
  ),
);

const filtered = computed(() => {
  const needle = query.value.trim().toLowerCase();
  let rows = entities.value;
  if (typeFilter.value) rows = rows.filter((r) => r.type === typeFilter.value);
  if (needle)
    rows = rows.filter((row) =>
      [row.id, row.name, row.description, row.tags].some((field) =>
        String(field ?? "").toLowerCase().includes(needle),
      ),
    );
  const key = sortKey.value;
  return [...rows].sort((a, b) =>
    String(a[key] ?? "").localeCompare(String(b[key] ?? ""), "zh"),
  );
});

async function load(): Promise<void> {
  try {
    const body = await apiGet<{ inventory: { entities: EntityRow[] } }>(
      `/projects/${currentProject()}/archive`,
    );
    entities.value = body.inventory.entities;
  } catch (e) {
    notifyError(e);
  }
}
onMounted(load);

// ---- detail / edit drawer ----
const detail = ref<EntityRow | null>(null);
const edit = ref({ name: "", description: "", tags: "" });
const saving = ref(false);
const showRaw = ref(false);

const profileRows = computed(() => {
  const p = detail.value?.metadata?.profile;
  if (!p || typeof p !== "object") return [];
  return Object.entries(p as Record<string, unknown>)
    .filter(([, v]) => v != null && v !== "")
    .map(([k, v]) => ({ k: PROFILE_LABELS[k] ?? k, v: String(v) }));
});

// ---- WS-I asset linking (attach existing media references; no AI image gen) ----
interface AssetRow {
  id: string;
  kind: string;
  uri: string;
  title: string;
}
const KIND_LABELS: Record<string, string> = {
  image: "图像",
  audio: "音频",
  map: "地图",
  link: "链接",
};
const assets = ref<AssetRow[]>([]);
const newAsset = ref({ kind: "image", uri: "", title: "" });

async function loadAssets(ref_: string): Promise<void> {
  assets.value = [];
  try {
    const body = await apiGet<{ assets: AssetRow[] }>(
      `/projects/${currentProject()}/assets?object_ref=${encodeURIComponent(ref_)}`,
    );
    assets.value = body.assets;
  } catch (e) {
    notifyError(e);
  }
}

async function attachAsset(): Promise<void> {
  const row = detail.value;
  if (!row || !newAsset.value.uri.trim()) return;
  try {
    await apiPost(`/projects/${currentProject()}/assets:attach`, {
      object_ref: `entity:${row.id}`,
      kind: newAsset.value.kind,
      uri: newAsset.value.uri.trim(),
      title: newAsset.value.title.trim(),
    });
    newAsset.value = { kind: "image", uri: "", title: "" };
    await loadAssets(`entity:${row.id}`);
  } catch (e) {
    notifyError(e);
  }
}

async function detachAsset(id: string): Promise<void> {
  const row = detail.value;
  if (!row) return;
  try {
    await apiPost(`/projects/${currentProject()}/assets:detach?asset_id=${encodeURIComponent(id)}`, {});
    await loadAssets(`entity:${row.id}`);
  } catch (e) {
    notifyError(e);
  }
}

function open(row: EntityRow): void {
  detail.value = row;
  edit.value = { name: row.name, description: row.description, tags: row.tags };
  showRaw.value = false;
  void loadAssets(`entity:${row.id}`);
}

async function save(): Promise<void> {
  const row = detail.value;
  if (!row || saving.value) return;
  saving.value = true;
  try {
    await apiPatch(`/projects/${currentProject()}/entities/${row.id}`, {
      name: edit.value.name.trim() || undefined,
      description: edit.value.description,
      tags: edit.value.tags
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
    });
    notifyOk("已保存，手改即时署名入正典。");
    detail.value = null;
    await load();
  } catch (e) {
    notifyError(e);
  } finally {
    saving.value = false;
  }
}

async function remove(): Promise<void> {
  const row = detail.value;
  if (!row) return;
  if (!window.confirm(`删除「${row.name}」？相关关系会一并清理，且不可撤销。`)) return;
  try {
    await apiDelete(`/projects/${currentProject()}/objects/entity/${row.id}`);
    notifyOk(`已删除「${row.name}」。`);
    detail.value = null;
    await load();
  } catch (e) {
    notifyError(e);
  }
}
</script>

<template>
  <section>
    <PageHead overline="ARCHIVE" title="设定档案" purpose="已入档实体，可查阅、过滤与编辑。" />

    <div class="toolbar">
      <input v-model="query" class="search" placeholder="搜索名称 / ID / 描述 / 标签…" />
      <select v-model="typeFilter" class="sel">
        <option value="">全部类型</option>
        <option v-for="t in presentTypes" :key="t" :value="t">{{ typeLabel(t) }}</option>
      </select>
      <select v-model="sortKey" class="sel">
        <option value="name">按名称</option>
        <option value="type">按类型</option>
        <option value="id">按 ID</option>
      </select>
      <span class="muted count">{{ filtered.length }} / {{ entities.length }}</span>
    </div>

    <div class="pane tablewrap">
      <table>
        <thead>
          <tr>
            <th>名称</th>
            <th>类型</th>
            <th>ID</th>
            <th>描述</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in filtered" :key="row.id" class="row" @click="open(row)">
            <td class="name">{{ row.name }}</td>
            <td><span class="type-chip">{{ typeLabel(row.type) }}</span></td>
            <td class="mono">{{ row.id }}</td>
            <td class="muted desc">{{ row.description }}</td>
          </tr>
          <tr v-if="!filtered.length">
            <td colspan="4" class="muted empty">没有匹配的条目。</td>
          </tr>
        </tbody>
      </table>
    </div>

    <Modal
      :open="detail !== null"
      :overline="detail ? typeLabel(detail.type) : ''"
      :title="detail?.name ?? ''"
      @close="detail = null"
    >
      <div v-if="detail" class="d-form">
        <div class="d-meta">
          <span class="mono">{{ detail.id }}</span>
          <span class="d-tag">{{ detail.origin }}</span>
          <span class="d-tag">{{ detail.review_status }}</span>
        </div>
        <label class="f">
          <span>名称</span>
          <input v-model="edit.name" maxlength="120" />
        </label>
        <label class="f">
          <span>描述</span>
          <textarea v-model="edit.description" rows="4"></textarea>
        </label>
        <label class="f">
          <span>标签<i class="muted">（逗号分隔）</i></span>
          <input v-model="edit.tags" placeholder="例如：港口, 走私, 中立" />
        </label>

        <template v-if="profileRows.length">
          <div class="section sub"><span class="t">角色卡</span></div>
          <div class="d-rows">
            <div v-for="(r, i) in profileRows" :key="i" class="d-row">
              <span class="rk">{{ r.k }}</span><span class="rv">{{ r.v }}</span>
            </div>
          </div>
        </template>

        <div class="section sub"><span class="t">关联素材</span></div>
        <p class="muted asset-hint">挂接已有的概念图 / 音频 / 地图 / 链接（填路径或 URL，不生成图）。</p>
        <div v-if="assets.length" class="asset-list">
          <div v-for="a in assets" :key="a.id" class="asset-row">
            <span class="asset-kind">{{ KIND_LABELS[a.kind] ?? a.kind }}</span>
            <span class="asset-title">{{ a.title || a.uri }}</span>
            <span class="mono asset-uri">{{ a.uri }}</span>
            <button class="asset-x" type="button" title="解除" @click="detachAsset(a.id)">×</button>
          </div>
        </div>
        <div class="asset-add">
          <select v-model="newAsset.kind" class="sel">
            <option value="image">图像</option>
            <option value="audio">音频</option>
            <option value="map">地图</option>
            <option value="link">链接</option>
          </select>
          <input v-model="newAsset.uri" placeholder="art/x.png 或 https://…" />
          <input v-model="newAsset.title" placeholder="标题（可选）" class="asset-title-in" />
          <button class="ghost" type="button" :disabled="!newAsset.uri.trim()" @click="attachAsset">
            挂接
          </button>
        </div>

        <button class="rawtoggle" type="button" @click="showRaw = !showRaw">
          {{ showRaw ? "收起原始数据" : "查看原始数据（对应文件）" }}
        </button>
        <pre v-if="showRaw" class="raw">{{ JSON.stringify(detail, null, 2) }}</pre>
      </div>
      <template #footer>
        <button class="ghost danger" @click="remove">删除</button>
        <span class="spacer"></span>
        <button class="ghost" @click="detail = null">关闭</button>
        <button class="primary" :disabled="saving" @click="save">{{ saving ? "保存中…" : "保存" }}</button>
      </template>
    </Modal>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  margin-bottom: 0.7rem;
  flex-wrap: wrap;
}
.search {
  flex: 1;
  min-width: 12rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.7rem;
}
.sel {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.84rem;
}
.count {
  font-size: 0.78rem;
}

.tablewrap {
  overflow-x: auto;
  padding: 0.2rem 0.6rem;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.86rem;
}
th {
  text-align: left;
  color: var(--ow-muted);
  font-weight: 500;
  padding: 0.5rem 0.6rem;
  border-bottom: 1px solid var(--ow-line);
}
td {
  padding: 0.45rem 0.6rem;
  border-bottom: 1px solid rgba(46, 54, 88, 0.55);
  vertical-align: top;
}
.row {
  cursor: pointer;
  transition: background 0.12s ease;
}
.row:hover {
  background: rgba(143, 214, 232, 0.06);
}
.name {
  color: var(--ow-ink);
  font-weight: 600;
}
.type-chip {
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  padding: 0.06rem 0.5rem;
  font-size: 0.76rem;
  color: var(--ow-cyan);
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.78rem;
}
.desc {
  max-width: 30rem;
}
.empty {
  text-align: center;
  padding: 1.5rem;
}

/* detail drawer */
.d-form {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
}
.d-meta {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  flex-wrap: wrap;
}
.d-tag {
  border: 1px solid var(--ow-gold-soft);
  border-radius: 999px;
  color: var(--ow-gold-bright);
  font-size: 0.72rem;
  padding: 0.08rem 0.5rem;
}
.f {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.f span {
  font-size: 0.8rem;
  color: var(--ow-muted);
}
.f input,
.f textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
  resize: vertical;
}
.d-rows {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 0.2rem 0.8rem;
}
.d-row {
  display: contents;
}
.rk {
  color: var(--ow-muted);
  font-size: 0.82rem;
}
.rv {
  font-size: 0.88rem;
  line-height: 1.6;
}
.asset-hint {
  font-size: 0.78rem;
  margin: -0.3rem 0 0;
}
.asset-list {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}
.asset-row {
  display: flex;
  align-items: baseline;
  gap: 0.55rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  background: var(--ow-panel-2);
  padding: 0.35rem 0.6rem;
  font-size: 0.82rem;
}
.asset-kind {
  color: var(--ow-cyan);
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  padding: 0.02rem 0.45rem;
  font-size: 0.72rem;
}
.asset-title {
  color: var(--ow-ink);
}
.asset-uri {
  margin-left: auto;
  max-width: 14rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.asset-x {
  border: none;
  background: transparent;
  color: var(--ow-muted);
  font-size: 1.1rem;
  line-height: 1;
  padding: 0 0.2rem;
  cursor: pointer;
}
.asset-x:hover {
  color: #e89a9a;
}
.asset-add {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
}
.asset-add input {
  flex: 1;
  min-width: 8rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.4rem 0.6rem;
  font: inherit;
  font-size: 0.82rem;
}
.asset-title-in {
  max-width: 9rem;
}
.rawtoggle {
  align-self: flex-start;
  background: transparent;
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.78rem;
  padding: 0.3rem 0.6rem;
  cursor: pointer;
}
.rawtoggle:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}
.raw {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  background: var(--ow-night-deep);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  padding: 0.6rem 0.7rem;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  line-height: 1.5;
  color: var(--ow-cyan);
}

button {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.9rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.86rem;
}
button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}
button.primary:disabled {
  opacity: 0.55;
}
button.ghost:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}
button.danger {
  color: #e89a9a;
}
button.danger:hover {
  border-color: rgba(224, 133, 133, 0.5);
  color: #e89a9a;
}
.spacer {
  flex: 1;
}
</style>
