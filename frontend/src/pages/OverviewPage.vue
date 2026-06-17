<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import { humanizeError, apiGet, apiPatch, currentProject } from "../api";
import { notifyError, notifyOk } from "../toast";
import ReadinessBoard from "../components/ReadinessBoard.vue";
import PageHead from "../components/PageHead.vue";
import Modal from "../components/Modal.vue";
import EmptyState from "../components/EmptyState.vue";

interface Overview {
  counts: Record<string, number>;
  graph: { nodes: number; edges: number };
  provenance?: { by_origin?: Record<string, number>; by_review_status?: Record<string, number> };
}

interface Inventory {
  entities: { id: string; name: string; type: string; description: string }[];
  quests: { id: string; title: string; objective: string; location: string; giver_npc: string }[];
  regions: { id: string; name: string; themes: string; level_min: number; level_max: number }[];
  pois: { id: string; name: string; purpose: string; region_id: string }[];
  dialogues: { id: string; text: string; text_key: string; speaker_id: string }[];
  relations: { source: string; kind: string; target: string }[];
  graph_refs: string[];
  style_guides: { id: string; body: string; rules: string[] }[];
}

interface Row {
  head: string;
  sub?: string;
  tag?: string;
  mono?: string;
}

const overview = ref<Overview | null>(null);
const error = ref("");

const TYPE_LABELS: Record<string, string> = {
  npc: "角色",
  location: "地点",
  faction: "势力",
  item: "物品",
  region: "区域",
  term: "术语",
  skill: "技能",
  achievement: "成就",
};

// Each tile is a window into the world. Clicking one pulls the real list — entities, quests,
// relations… — from the archive into a modal, so the overview is a launchpad, not just a scoreboard.
interface Tile {
  key: string;
  label: string;
  overline: string;
  rows: (inv: Inventory) => Row[];
}
const TILES: Tile[] = [
  {
    key: "entities", label: "实体", overline: "ENTITIES",
    rows: (inv) => inv.entities.map((e) => ({
      head: e.name, sub: e.description, tag: TYPE_LABELS[e.type] ?? e.type, mono: e.id,
    })),
  },
  {
    key: "quests", label: "任务", overline: "QUESTS",
    rows: (inv) => inv.quests.map((q) => ({
      head: q.title, sub: q.objective, tag: q.location || q.giver_npc || "", mono: q.id,
    })),
  },
  {
    key: "regions", label: "区域", overline: "REGIONS",
    rows: (inv) => inv.regions.map((r) => ({
      head: r.name, sub: r.themes, tag: r.level_max ? `Lv ${r.level_min}-${r.level_max}` : "", mono: r.id,
    })),
  },
  {
    key: "relations", label: "关系", overline: "RELATIONS",
    rows: (inv) => inv.relations.map((rel) => ({
      head: `${rel.source} → ${rel.target}`, tag: rel.kind,
    })),
  },
  {
    key: "pois", label: "兴趣点", overline: "POINTS",
    rows: (inv) => inv.pois.map((p) => ({
      head: p.name, sub: p.purpose, tag: p.region_id || "", mono: p.id,
    })),
  },
  {
    key: "dialogues", label: "对白", overline: "DIALOGUE",
    rows: (inv) => inv.dialogues.map((d) => ({
      head: d.text || d.text_key, sub: d.speaker_id ? `— ${d.speaker_id}` : "", mono: d.id,
    })),
  },
  {
    key: "nodes", label: "图谱节点", overline: "NODES",
    rows: (inv) => inv.graph_refs.map((ref) => ({ head: ref, mono: ref })),
  },
  {
    key: "edges", label: "图谱边", overline: "EDGES",
    rows: (inv) => inv.relations.map((rel) => ({
      head: `${rel.source} → ${rel.target}`, tag: rel.kind,
    })),
  },
];
const countOf = (key: string): number => {
  if (key === "nodes") return overview.value?.graph.nodes ?? 0;
  if (key === "edges") return overview.value?.graph.edges ?? 0;
  return overview.value?.counts[key] ?? 0;
};

// ---- drill-down modal ----
const inventory = ref<Inventory | null>(null);
const invLoading = ref(false);
const invError = ref("");
const activeKey = ref<string | null>(null);
const activeTile = computed(() => TILES.find((t) => t.key === activeKey.value) ?? null);
const drillRows = computed<Row[]>(() =>
  activeTile.value && inventory.value ? activeTile.value.rows(inventory.value) : [],
);

async function openTile(tile: Tile): Promise<void> {
  if (countOf(tile.key) === 0) return;
  activeKey.value = tile.key;
  await ensureInventory();
}
function closeTile(): void {
  activeKey.value = null;
}

async function ensureInventory(): Promise<void> {
  if (inventory.value || invLoading.value) return;
  invLoading.value = true;
  invError.value = "";
  try {
    const body = await apiGet<{ inventory: Inventory }>(`/projects/${currentProject()}/archive`);
    inventory.value = body.inventory;
  } catch (e) {
    invError.value = humanizeError(e);
  } finally {
    invLoading.value = false;
  }
}

// expandable worldview panel — the full style guide / 世界观设定 the world was built on
const worldviewOpen = ref(false);
async function toggleWorldview(): Promise<void> {
  worldviewOpen.value = !worldviewOpen.value;
  if (worldviewOpen.value) await ensureInventory();
}
const styleGuides = computed(() => inventory.value?.style_guides ?? []);

// B10 · inline-edit the worldview style guide (body + rules) — lands immediately (human edit)
const editingGuide = ref<string | null>(null);
const guideEdit = reactive({ body: "", rules: "" });
const savingGuide = ref(false);
function startEditGuide(g: { id: string; body: string; rules: string[] }): void {
  editingGuide.value = g.id;
  guideEdit.body = g.body;
  guideEdit.rules = g.rules.join("\n");
}
async function saveGuide(): Promise<void> {
  if (!editingGuide.value || savingGuide.value) return;
  savingGuide.value = true;
  try {
    await apiPatch(`/projects/${currentProject()}/style_guides/${editingGuide.value}`, {
      body: guideEdit.body,
      rules: guideEdit.rules.split("\n").map((r) => r.trim()).filter(Boolean),
    });
    notifyOk("世界观设定已保存，即时入正典。");
    editingGuide.value = null;
    inventory.value = null;
    await ensureInventory();
  } catch (e) {
    notifyError(e);
  } finally {
    savingGuide.value = false;
  }
}

// count-up: rAF tween toward each tile's real value, honoring reduced-motion
const shown = reactive<Record<string, number>>({});
let raf = 0;

function tweenTo(targets: Record<string, number>): void {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    Object.assign(shown, targets);
    return;
  }
  const start = performance.now();
  const from: Record<string, number> = { ...shown };
  const duration = 700;
  const step = (now: number): void => {
    const t = Math.min(1, (now - start) / duration);
    const ease = 1 - (1 - t) ** 3;
    for (const key of Object.keys(targets)) {
      shown[key] = Math.round((from[key] ?? 0) + ((targets[key] ?? 0) - (from[key] ?? 0)) * ease);
    }
    if (t < 1) raf = window.requestAnimationFrame(step);
  };
  raf = window.requestAnimationFrame(step);
}

onUnmounted(() => window.cancelAnimationFrame(raf));

onMounted(async () => {
  try {
    const body = await apiGet<{ overview: Overview }>(
      `/projects/${currentProject()}/overview`,
    );
    overview.value = body.overview;
    const targets: Record<string, number> = {};
    for (const tile of TILES) targets[tile.key] = countOf(tile.key);
    tweenTo(targets);
  } catch (e) {
    error.value = humanizeError(e);
  }
});
</script>

<template>
  <section>
    <PageHead overline="OVERVIEW" title="世界总览" purpose="世界的规模、来源与就绪度。点任一指标可展开明细。" />

    <div v-if="error" class="empty-state pane">
      <p>{{ error }}</p>
      <RouterLink to="/worlds" class="empty-cta">前往「管理 · 世界」</RouterLink>
    </div>
    <div v-else-if="!overview" class="tiles">
      <div v-for="i in 8" :key="i" class="tile pane skeleton">
        <span class="sk sk-label"></span>
        <span class="sk sk-value"></span>
      </div>
    </div>
    <template v-else>
      <div class="tiles stagger">
        <button
          v-for="tile in TILES"
          :key="tile.key"
          class="tile pane"
          :class="{ empty: countOf(tile.key) === 0 }"
          :disabled="countOf(tile.key) === 0"
          @click="openTile(tile)"
        >
          <span class="ov-line">{{ tile.overline }}</span>
          <span class="label">{{ tile.label }}</span>
          <span class="value num">{{ shown[tile.key] ?? 0 }}</span>
          <span v-if="countOf(tile.key) > 0" class="tile-go" aria-hidden="true"></span>
        </button>
      </div>

      <div class="section"><span class="t">内容溯源</span></div>
      <div class="chips">
        <span
          v-for="(count, origin) in overview.provenance?.by_origin ?? {}"
          :key="origin"
          class="chip"
        >
          {{ origin }} <b>{{ count }}</b>
        </span>
        <span
          v-for="(count, status) in overview.provenance?.by_review_status ?? {}"
          :key="status"
          class="chip"
        >
          {{ status }} <b>{{ count }}</b>
        </span>
      </div>

      <!-- expandable worldview: the full style guide the world was generated against -->
      <button class="wv-toggle" :aria-expanded="worldviewOpen" @click="toggleWorldview">
        <span class="wv-caret" :class="{ open: worldviewOpen }"></span>
        世界观设定
        <span class="muted small">{{ worldviewOpen ? "" : "展开查看完整设定" }}</span>
      </button>
      <Transition name="wv">
        <div v-if="worldviewOpen" class="wv-body">
          <div v-if="invLoading" class="drill-load">
            <span class="ow-spinner"></span><span class="muted">载入中…</span>
          </div>
          <p v-else-if="!styleGuides.length" class="muted">这个世界还没有写下世界观设定。</p>
          <div v-for="g in styleGuides" v-else :key="g.id" class="wv-guide pane">
            <template v-if="editingGuide === g.id">
              <label class="wv-f">
                <span>世界观正文</span>
                <textarea v-model="guideEdit.body" rows="6"></textarea>
              </label>
              <label class="wv-f">
                <span>设定守则<i class="muted">（每行一条）</i></span>
                <textarea v-model="guideEdit.rules" rows="4"></textarea>
              </label>
              <div class="wv-actions">
                <button class="primary" :disabled="savingGuide" @click="saveGuide">
                  {{ savingGuide ? "保存中…" : "保存" }}
                </button>
                <button class="ghost" @click="editingGuide = null">取消</button>
              </div>
            </template>
            <template v-else>
              <div class="wv-top">
                <button class="wv-edit ghost" @click="startEditGuide(g)">编辑</button>
              </div>
              <p v-if="g.body" class="wv-text">{{ g.body }}</p>
              <template v-if="g.rules.length">
                <div class="section sub"><span class="t">设定守则</span></div>
                <ul class="wv-rules">
                  <li v-for="(rule, i) in g.rules" :key="i">{{ rule }}</li>
                </ul>
              </template>
            </template>
          </div>
        </div>
      </Transition>

      <ReadinessBoard />
    </template>

    <Modal
      :open="activeKey !== null"
      :overline="activeTile?.overline ?? ''"
      :title="activeTile?.label ?? ''"
      :count="activeTile ? countOf(activeTile.key) : 0"
      wide
      @close="closeTile"
    >
      <div v-if="invLoading" class="drill-load">
        <span class="ow-spinner"></span><span class="muted">正在载入明细…</span>
      </div>
      <p v-else-if="invError" class="muted">{{ invError }}</p>
      <EmptyState v-else-if="!drillRows.length" title="暂无内容" />
      <ul v-else class="drill reveal">
        <li v-for="(row, i) in drillRows" :key="i" class="drill-row">
          <div class="dr-main">
            <span class="dr-head">{{ row.head }}</span>
            <span v-if="row.sub" class="dr-sub muted">{{ row.sub }}</span>
          </div>
          <span v-if="row.tag" class="dr-tag">{{ row.tag }}</span>
          <span v-if="row.mono" class="dr-id mono">{{ row.mono }}</span>
        </li>
      </ul>
    </Modal>
  </section>
</template>

<style scoped>
.empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.8rem;
  padding: 2.4rem 1.5rem;
  text-align: center;
  color: var(--ow-muted);
}
.empty-state p {
  margin: 0;
  max-width: 30rem;
  line-height: 1.6;
}
.empty-cta {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
  border-radius: 0.5rem;
  padding: 0.45rem 1rem;
  font-size: 0.85rem;
  text-decoration: none;
}
.empty-cta:hover {
  border-color: var(--ow-gold);
}

.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 0.7rem;
  margin-bottom: 1.1rem;
}

.tile {
  padding: 0.7rem 0.95rem 0.7rem;
  display: flex;
  flex-direction: column;
  gap: 0.12rem;
  text-align: left;
  cursor: pointer;
  font: inherit;
  color: inherit;
  position: relative;
}
.tile.empty {
  cursor: default;
  opacity: 0.62;
}
.tile:not(.empty):hover {
  border-color: rgba(240, 210, 138, 0.55);
}
.tile:not(.empty):hover .tile-go {
  opacity: 1;
  transform: translateX(2px);
}
/* a small corner arrow that says "this opens" — only on populated tiles */
.tile-go {
  position: absolute;
  right: 0.85rem;
  bottom: 0.8rem;
  width: 7px;
  height: 7px;
  border-top: 1.5px solid var(--ow-gold);
  border-right: 1.5px solid var(--ow-gold);
  transform: rotate(45deg);
  opacity: 0.5;
  transition: opacity 0.18s ease, transform 0.18s ease;
}

.ov-line {
  font-family: var(--ow-overline);
  font-size: 0.6rem;
  letter-spacing: 0.16em;
  color: var(--ow-violet);
  opacity: 0.85;
}
.label {
  color: var(--ow-muted);
  font-size: 0.8rem;
  letter-spacing: 0.06em;
}
.value {
  color: var(--ow-gold-bright);
  font-size: 1.7rem;
  line-height: 1.15;
}

/* skeleton shimmer while the archive loads */
.sk {
  display: block;
  border-radius: 0.35rem;
  background: linear-gradient(
    100deg,
    rgba(46, 54, 88, 0.45) 40%,
    rgba(217, 181, 108, 0.18) 50%,
    rgba(46, 54, 88, 0.45) 60%
  );
  background-size: 220% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}
.sk-label {
  width: 3.2rem;
  height: 0.78rem;
}
.sk-value {
  width: 2.4rem;
  height: 1.7rem;
  margin-top: 0.2rem;
}
@keyframes shimmer {
  to {
    background-position: -120% 0;
  }
}

/* tiles drift in one after another */
.stagger > .tile {
  animation: rise 0.45s ease both;
}
.stagger > .tile:nth-child(2) { animation-delay: 0.05s; }
.stagger > .tile:nth-child(3) { animation-delay: 0.1s; }
.stagger > .tile:nth-child(4) { animation-delay: 0.15s; }
.stagger > .tile:nth-child(5) { animation-delay: 0.2s; }
.stagger > .tile:nth-child(6) { animation-delay: 0.25s; }
.stagger > .tile:nth-child(7) { animation-delay: 0.3s; }
.stagger > .tile:nth-child(8) { animation-delay: 0.35s; }
@keyframes rise {
  from { opacity: 0; transform: translateY(8px); }
  to { opacity: 1; transform: translateY(0); }
}
@media (prefers-reduced-motion: reduce) {
  .sk,
  .stagger > .tile {
    animation: none;
  }
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.chip {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  border-radius: 999px;
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
  padding: 0.14rem 0.62rem;
}
.chip b {
  color: var(--ow-ink);
}

/* ---- worldview panel ---- */
.wv-toggle {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  width: 100%;
  margin: 1rem 0 0;
  padding: 0.5rem 0;
  background: transparent;
  border: 0;
  border-top: 1px solid var(--ow-gold-faint);
  color: var(--ow-ink);
  font: inherit;
  font-family: var(--ow-display);
  font-weight: 700;
  font-size: 1.02rem;
  letter-spacing: 0.04em;
  cursor: pointer;
}
.wv-caret {
  width: 6px;
  height: 6px;
  border-right: 1.5px solid var(--ow-gold);
  border-bottom: 1.5px solid var(--ow-gold);
  transform: rotate(-45deg);
  transition: transform 0.22s ease;
}
.wv-caret.open {
  transform: rotate(45deg);
}
.wv-toggle .small {
  margin-left: auto;
  font-size: 0.78rem;
  font-weight: 400;
  font-family: var(--ow-display);
}
.wv-body {
  margin: 0.3rem 0 0.6rem;
}
.wv-guide {
  padding: 0.9rem 1.1rem;
  margin-bottom: 0.6rem;
}
.wv-top {
  display: flex;
  justify-content: flex-end;
}
.wv-edit {
  font-size: 0.78rem;
  padding: 0.2rem 0.6rem;
}
.wv-f {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.6rem;
}
.wv-f > span {
  font-size: 0.82rem;
  color: var(--ow-muted);
}
.wv-f textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.55rem 0.7rem;
  font: inherit;
  font-size: 0.88rem;
  line-height: 1.7;
  resize: vertical;
}
.wv-actions {
  display: flex;
  gap: 0.5rem;
}
.wv-text {
  margin: 0;
  line-height: 1.8;
  font-size: 0.92rem;
  white-space: pre-wrap;
}
.wv-rules {
  margin: 0;
  padding-left: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  font-size: 0.86rem;
  line-height: 1.6;
}
.wv-enter-active,
.wv-leave-active {
  transition: opacity 0.25s ease;
}
.wv-enter-from,
.wv-leave-to {
  opacity: 0;
}

/* ---- drill-down list inside the modal ---- */
.drill-load {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 1.2rem 0;
}
.drill {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
}
.drill-row {
  display: flex;
  align-items: baseline;
  gap: 0.7rem;
  padding: 0.5rem 0.6rem;
  border-radius: 0.4rem;
  border-left: 2px solid var(--ow-violet-soft);
  background: rgba(143, 214, 232, 0.04);
}
.dr-main {
  display: flex;
  flex-direction: column;
  gap: 0.12rem;
  min-width: 0;
  flex: 1;
}
.dr-head {
  color: var(--ow-ink);
  font-size: 0.9rem;
  font-weight: 500;
}
.dr-sub {
  font-size: 0.78rem;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}
.dr-tag {
  flex: none;
  font-size: 0.72rem;
  color: var(--ow-gold-bright);
  border: 1px solid var(--ow-gold-soft);
  border-radius: 999px;
  padding: 0.06rem 0.5rem;
}
.dr-id {
  flex: none;
  font-size: 0.72rem;
  color: var(--ow-cyan);
  opacity: 0.8;
}
</style>
