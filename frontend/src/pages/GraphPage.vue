<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import Graph3D from "../components/Graph3D.vue";
import {
  type GraphEdge,
  type GraphNode,
  EDGE_CAT_META,
  GRAPH_LEGEND,
  edgeCat,
  kindColor,
} from "../graphTheme";
import { apiDelete, apiGet, apiPatch, apiPost, currentProject } from "../api";
import { notifyError, notifyOk } from "../toast";
import PageHead from "../components/PageHead.vue";

// 关系星图（可编辑，3D）：按「智库」分库看（人物/阵营/地点/事件/概念），连线按关系类别区分
// （所属/人际/邦交/地理/叙事）。点节点开详情面板改/删/连线——与设定档案手改同源的人工编辑管线。
interface ArchiveEntity {
  id: string;
  name: string;
  type: string;
  description: string;
  tags: string;
}
interface GraphPayload {
  nodes: GraphNode[];
  edges: GraphEdge[];
}
interface RelationKind {
  id: string;
  label: string;
  category: string;
  symmetric: boolean;
}

const TYPE_LABEL: Record<string, string> = {
  faction: "阵营",
  npc: "角色",
  location: "地点",
  region: "区域",
  event: "事件",
  item: "物品",
  organization: "组织",
  concept: "概念",
  poi: "地点",
  quest: "任务",
  term: "术语",
};
const NEW_TYPES = ["npc", "faction", "location", "item", "event", "organization", "concept"];

// 智库式分库：每个视图聚焦一类节点，把混在一起的大图拆成读得清的小图。
const VIEWS: { key: string; label: string; kinds: string[] | null }[] = [
  { key: "all", label: "全部", kinds: null },
  { key: "people", label: "人物关系", kinds: ["npc", "faction"] },
  { key: "faction", label: "阵营关系", kinds: ["faction"] },
  { key: "place", label: "地点", kinds: ["location", "poi", "region"] },
  { key: "event", label: "事件", kinds: ["event", "npc", "faction"] },
  { key: "concept", label: "概念", kinds: ["term", "concept", "item", "skill", "achievement"] },
];
const EDGE_LEGEND = (
  ["affiliation", "interpersonal", "alliance", "geography", "narrative"] as const
).map((k) => ({ k, ...EDGE_CAT_META[k] }));

const entities = ref<ArchiveEntity[]>([]);
const relationKinds = ref<RelationKind[]>([]);
const full = ref<GraphPayload | null>(null);
const view = ref("all");
const selected = ref("");
const loading = ref(false);
const fullscreen = ref(false);

const edit = ref({ name: "", description: "", tags: "" });
const rel = ref({ target: "", kind: "ally_of", custom: "", symmetric: false });
const adding = ref(false);
const newNode = ref({ name: "", type: "npc", description: "" });

const proj = (): string => currentProject();

const viewKinds = computed(() => VIEWS.find((v) => v.key === view.value)?.kinds ?? null);
const nodes = computed<GraphNode[]>(() => {
  const k = viewKinds.value;
  return (full.value?.nodes ?? []).filter((n) => !k || k.includes(n.kind));
});
const visibleRefs = computed(() => new Set(nodes.value.map((n) => n.ref)));
const edges = computed<GraphEdge[]>(() =>
  (full.value?.edges ?? []).filter(
    (e) => visibleRefs.value.has(e.source) && visibleRefs.value.has(e.target),
  ),
);

const focusOptions = computed(() =>
  [...entities.value]
    .sort((a, b) => a.type.localeCompare(b.type) || a.name.localeCompare(b.name))
    .map((e) => ({ value: `entity:${e.id}`, label: `${TYPE_LABEL[e.type] ?? e.type} · ${e.name}` })),
);

const selectedNode = computed(() => full.value?.nodes.find((n) => n.ref === selected.value));
const selectedIsEntity = computed(() => selected.value.startsWith("entity:"));
const selectedId = computed(() => selected.value.split(":")[1] ?? "");
const relTargets = computed(() => focusOptions.value.filter((o) => o.value !== selected.value));

const selectedRelations = computed(() =>
  (full.value?.edges ?? [])
    .filter((e) => e.source === selected.value || e.target === selected.value)
    .map((e) => {
      const otherRef = e.source === selected.value ? e.target : e.source;
      return {
        ...e,
        otherRef,
        otherLabel: full.value?.nodes.find((n) => n.ref === otherRef)?.label ?? otherRef,
        outgoing: e.source === selected.value,
      };
    }),
);

async function loadGraph(): Promise<void> {
  loading.value = true;
  try {
    // always the whole-world graph; the 智库 tabs filter it client-side
    const body = await apiGet<{ graph: GraphPayload }>(`/projects/${proj()}/graph`);
    full.value = body.graph;
    if (selected.value && !full.value.nodes.some((n) => n.ref === selected.value)) selected.value = "";
  } catch (e) {
    notifyError(e);
  } finally {
    loading.value = false;
  }
}

async function reloadArchive(): Promise<void> {
  const body = await apiGet<{ inventory: { entities: ArchiveEntity[] } }>(
    `/projects/${proj()}/archive`,
  );
  entities.value = body.inventory.entities;
}

function onSelect(ref: string): void {
  selected.value = ref;
  const node = full.value?.nodes.find((n) => n.ref === ref);
  const ent = entities.value.find((e) => `entity:${e.id}` === ref);
  edit.value = { name: node?.label ?? "", description: ent?.description ?? "", tags: ent?.tags ?? "" };
}

async function saveEdit(): Promise<void> {
  if (!selectedIsEntity.value) return;
  try {
    await apiPatch(`/projects/${proj()}/entities/${encodeURIComponent(selectedId.value)}`, {
      name: edit.value.name,
      description: edit.value.description,
      tags: edit.value.tags
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
    });
    notifyOk("已保存。");
    await reloadArchive();
    await loadGraph();
  } catch (e) {
    notifyError(e);
  }
}

async function deleteNode(): Promise<void> {
  if (!selectedNode.value) return;
  const refType = selectedIsEntity.value ? "entity" : selectedNode.value.kind === "quest" ? "quest" : "";
  if (!refType) {
    notifyError("这种节点暂不支持在图上删除。");
    return;
  }
  if (!window.confirm(`删除「${selectedNode.value.label}」？相关关系会一并移除。`)) return;
  try {
    await apiDelete(`/projects/${proj()}/objects/${refType}/${encodeURIComponent(selectedId.value)}`);
    notifyOk("已删除。");
    selected.value = "";
    await reloadArchive();
    await loadGraph();
  } catch (e) {
    notifyError(e);
  }
}

async function addRelation(): Promise<void> {
  const kind = rel.value.kind === "__custom__" ? rel.value.custom.trim() : rel.value.kind;
  const targetId = rel.value.target.split(":")[1] ?? "";
  if (!kind || !targetId) return;
  try {
    await apiPost(`/projects/${proj()}/relations`, {
      source: selectedId.value,
      target: targetId,
      kind,
      symmetric: rel.value.symmetric || null,
    });
    rel.value.custom = "";
    notifyOk("已连接关系。");
    await loadGraph();
  } catch (e) {
    notifyError(e);
  }
}

async function removeRelation(otherRef: string, kind: string, outgoing: boolean): Promise<void> {
  const a = outgoing ? selectedId.value : otherRef.split(":")[1] ?? "";
  const b = outgoing ? otherRef.split(":")[1] ?? "" : selectedId.value;
  try {
    const q = new URLSearchParams({ source: a, target: b, kind });
    await apiDelete(`/projects/${proj()}/relations?${q.toString()}`);
    notifyOk("已移除关系。");
    await loadGraph();
  } catch (e) {
    notifyError(e);
  }
}

async function createNode(): Promise<void> {
  if (!newNode.value.name.trim()) return;
  try {
    const body = await apiPost<{ entity: { id: string } }>(`/projects/${proj()}/entities`, {
      name: newNode.value.name.trim(),
      type: newNode.value.type,
      description: newNode.value.description.trim(),
    });
    notifyOk(`已新增「${newNode.value.name.trim()}」。`);
    newNode.value = { name: "", type: "npc", description: "" };
    adding.value = false;
    await reloadArchive();
    await loadGraph();
    onSelect(`entity:${body.entity.id}`);
  } catch (e) {
    notifyError(e);
  }
}

function relKindLabel(kind: string): string {
  return relationKinds.value.find((k) => k.id === kind)?.label ?? kind;
}
// colour a relation-row arrow by its category (所属/人际/邦交/…)
function relColor(kind: string | undefined): string {
  return EDGE_CAT_META[edgeCat(kind ?? "")].color;
}

onMounted(async () => {
  try {
    await reloadArchive();
    const rk = await apiGet<{ kinds: RelationKind[] }>(`/projects/${proj()}/relation_kinds`);
    relationKinds.value = rk.kinds;
    await loadGraph();
  } catch (e) {
    notifyError(e);
  }
});
</script>

<template>
  <section>
    <PageHead overline="GRAPH" title="关系星图" purpose="按智库分库看实体关系，点节点改名、连线。可旋转缩放的 3D 星图。" />

    <!-- 智库式分库标签 -->
    <div class="tabs">
      <button
        v-for="v in VIEWS"
        :key="v.key"
        class="tab"
        :class="{ on: view === v.key }"
        @click="view = v.key"
      >
        {{ v.label }}
      </button>
      <button class="add-btn" @click="adding = !adding">+ 新增节点</button>
    </div>

    <div v-if="adding" class="pane add-form">
      <input v-model="newNode.name" maxlength="120" placeholder="名字，如：灰渡" />
      <select v-model="newNode.type">
        <option v-for="t in NEW_TYPES" :key="t" :value="t">{{ TYPE_LABEL[t] ?? t }}</option>
      </select>
      <input v-model="newNode.description" maxlength="400" placeholder="一句话简介（可留空）" />
      <button class="primary" @click="createNode">创建</button>
      <button class="ghost" @click="adding = false">取消</button>
    </div>

    <!-- legend: node types + edge categories (membership vs interpersonal etc. now read distinctly) -->
    <div class="legend">
      <span class="leg-group">
        <span v-for="[kind, label] in GRAPH_LEGEND" :key="kind" class="leg">
          <i class="dot node" :style="{ background: kindColor(kind) }"></i>{{ label }}
        </span>
      </span>
      <span class="leg-sep"></span>
      <span class="leg-group">
        <span v-for="e in EDGE_LEGEND" :key="e.k" class="leg">
          <i class="dot edge" :style="{ background: e.color }"></i>{{ e.label }}
        </span>
      </span>
    </div>

    <div class="layout" :class="{ 'has-detail': selectedNode }">
      <div class="canvas-col">
        <div class="graph-stage" :class="{ full: fullscreen, loading }">
          <button class="stage-full" :title="fullscreen ? '退出全屏' : '放大视图'" @click="fullscreen = !fullscreen">
            {{ fullscreen ? "退出全屏" : "放大视图" }}
          </button>
          <p v-if="full && !nodes.length" class="muted empty">
            这一库还没有内容。换个分库，或新增节点。
          </p>
          <Graph3D v-else :nodes="nodes" :edges="edges" :selected="selected" @select="onSelect" />
        </div>
      </div>

      <div v-if="selectedNode" class="pane detail">
        <div class="d-head">
          <span class="d-gem" :style="{ background: kindColor(selectedNode.kind) }"></span>
          <b>{{ selectedNode.label }}</b>
          <span class="muted">{{ TYPE_LABEL[selectedNode.kind] ?? selectedNode.kind }}</span>
        </div>

        <template v-if="selectedIsEntity">
          <label class="f"><span>名字</span><input v-model="edit.name" maxlength="120" /></label>
          <label class="f"><span>简介</span><textarea v-model="edit.description" rows="3"></textarea></label>
          <label class="f"><span>标签（逗号分隔）</span><input v-model="edit.tags" /></label>
          <div class="d-actions">
            <button class="primary" @click="saveEdit">保存</button>
            <button class="danger" @click="deleteNode">删除</button>
          </div>
        </template>
        <template v-else>
          <p class="muted small">这种节点（{{ TYPE_LABEL[selectedNode.kind] }}）在对应工坊里编辑。</p>
          <div v-if="selectedNode.kind === 'quest'" class="d-actions">
            <button class="danger" @click="deleteNode">删除</button>
          </div>
        </template>

        <div class="section sub"><span class="t">关系</span></div>
        <div v-if="selectedRelations.length" class="rels">
          <div v-for="(r, i) in selectedRelations" :key="i" class="rel-row">
            <span class="dir" :style="{ color: relColor(r.kind) }">{{ r.outgoing ? "→" : "←" }}</span>
            <span class="rk">{{ r.label || relKindLabel(r.kind || "") }}</span>
            <span class="ro">{{ r.otherLabel }}</span>
            <button
              v-if="selectedIsEntity"
              class="x"
              title="移除关系"
              @click="removeRelation(r.otherRef, r.kind || '', r.outgoing)"
            >×</button>
          </div>
        </div>
        <p v-else class="muted small">还没有关系。</p>

        <template v-if="selectedIsEntity">
          <div class="add-rel">
            <select v-model="rel.target">
              <option value="">连到…</option>
              <option v-for="o in relTargets" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
            <select v-model="rel.kind">
              <optgroup
                v-for="cat in [...new Set(relationKinds.map((k) => k.category))]"
                :key="cat"
                :label="cat"
              >
                <option
                  v-for="k in relationKinds.filter((x) => x.category === cat)"
                  :key="k.id"
                  :value="k.id"
                >{{ k.label }}{{ k.symmetric ? "（双向）" : "" }}</option>
              </optgroup>
              <option value="__custom__">自定义…</option>
            </select>
            <input v-if="rel.kind === '__custom__'" v-model="rel.custom" maxlength="60" placeholder="自定义关系名" />
            <label v-if="rel.kind === '__custom__'" class="sym">
              <input v-model="rel.symmetric" type="checkbox" />双向
            </label>
            <button class="primary" :disabled="!rel.target" @click="addRelation">连接</button>
          </div>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.tabs {
  display: flex;
  gap: 0.4rem;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 0.6rem;
}
.tab {
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.86rem;
  padding: 0.4rem 0.95rem;
  cursor: pointer;
  transition: all 0.15s ease;
}
.tab.on {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.18);
}
.add-btn {
  margin-left: auto;
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: transparent;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.84rem;
  padding: 0.4rem 0.85rem;
  cursor: pointer;
}
.add-btn:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
}

.add-form {
  display: flex;
  gap: 0.5rem;
  align-items: center;
  padding: 0.7rem 1rem;
  margin-bottom: 0.6rem;
  flex-wrap: wrap;
}
.add-form input {
  flex: 1;
  min-width: 8rem;
}

.legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem 0.9rem;
  margin: 0.2rem 0 0.6rem;
  font-size: 0.78rem;
  color: var(--ow-muted);
}
.leg-group {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 0.7rem;
}
.leg-sep {
  width: 1px;
  height: 0.9rem;
  background: var(--ow-line);
}
.leg {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
}
.dot {
  display: inline-block;
}
.dot.node {
  width: 9px;
  height: 9px;
  border-radius: 50%;
}
.dot.edge {
  width: 14px;
  height: 3px;
  border-radius: 2px;
}

.layout {
  display: grid;
  grid-template-columns: 1fr;
  gap: 0.9rem;
  align-items: start;
}
.layout.has-detail {
  grid-template-columns: 1fr 17rem;
}
.canvas-col {
  min-width: 0;
}
/* a plain stage (NOT a .pane) so fullscreen position:fixed isn't trapped by a panel's filter */
.graph-stage {
  position: relative;
  height: clamp(440px, 64vh, 780px);
  border: 1px solid var(--ow-edge-violet);
  border-radius: 10px;
  background:
    radial-gradient(70% 60% at 78% 12%, rgba(160, 138, 255, 0.08), transparent 70%),
    linear-gradient(168deg, rgba(22, 29, 68, 0.5), rgba(10, 14, 36, 0.55));
  overflow: hidden;
  transition: opacity 0.15s ease;
}
.graph-stage.loading {
  opacity: 0.5;
}
.graph-stage.full {
  position: fixed;
  inset: 2vh 2vw;
  z-index: 7000;
  height: auto;
  background: linear-gradient(168deg, rgba(16, 21, 48, 0.98), rgba(8, 11, 28, 0.99));
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.6);
}
.stage-full {
  position: absolute;
  top: 0.6rem;
  right: 0.6rem;
  z-index: 2;
  border: 1px solid var(--ow-gold-soft);
  border-radius: 0.5rem;
  background: rgba(13, 18, 42, 0.7);
  color: var(--ow-gold-bright);
  font: inherit;
  font-size: 0.8rem;
  padding: 0.32rem 0.7rem;
  cursor: pointer;
}
.stage-full:hover {
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.25);
}
.empty {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
}

.detail {
  padding: 0.8rem 0.9rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.d-head {
  display: flex;
  align-items: center;
  gap: 0.45rem;
}
.d-gem {
  width: 11px;
  height: 11px;
  border-radius: 50%;
  display: inline-block;
}
.d-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}
.f {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.8rem;
  color: var(--ow-muted);
}
.f input,
.f textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.86rem;
}
button {
  border-radius: 0.5rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
  padding: 0.45rem 0.85rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
}
button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}
button.ghost {
  color: var(--ow-muted);
}
button.danger {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.d-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.2rem;
}
.sub {
  margin: 0.5rem 0 0.2rem;
}
.rels {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.rel-row {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.82rem;
}
.rk {
  color: var(--ow-gold-bright);
}
.ro {
  color: var(--ow-ink);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.x {
  padding: 0 0.4rem;
  color: #e89a9a;
  border-color: transparent;
  background: transparent;
}
.add-rel {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.4rem;
}
.add-rel select,
.add-rel input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.86rem;
}
.sym {
  font-size: 0.8rem;
  color: var(--ow-muted);
  display: flex;
  gap: 0.3rem;
  align-items: center;
}
.small {
  font-size: 0.78rem;
}

@media (max-width: 820px) {
  .layout {
    grid-template-columns: 1fr;
  }
}
@media (prefers-reduced-motion: reduce) {
  .graph-stage {
    transition: none;
  }
}
</style>
