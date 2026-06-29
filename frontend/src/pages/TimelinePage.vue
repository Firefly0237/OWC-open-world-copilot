<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { humanizeError, apiGet, apiPatch, apiPost, currentProject } from "../api";
import { notifyError } from "../toast";
import PageHead from "../components/PageHead.vue";
import ZoomCanvas from "../components/ZoomCanvas.vue";

// 编年校验 = 把任务/事件按 timeline_order 摆到一条轴上，违例由确定性审计标出。
// 现在可编辑：点星钻开右侧面板改顺序/标题/前置，或横向拖动落到最近的刻度列；改动即时写回正典。
interface TimelineItem {
  ref: string;
  kind: string;
  label: string;
  order: number;
  rank: number;
  flags: string[];
}
interface TimelineDependency {
  source: string;
  target: string;
  violation: boolean;
}
interface TimelineEntry {
  ref: string;
  kind: string;
  label: string;
}
interface TimelineView {
  items: TimelineItem[];
  dependencies: TimelineDependency[];
  unsequenced: TimelineEntry[];
  rank_count: number;
}

const COL = 120;
const BOX_H = 30; // vertical row metric — lanes/bands keep their height; markers are now gems
const MARGIN_LEFT = 64;
const ROW_GAP = 8;
const LANE_GAP = 30;
const GEM_R = 11; // star-gem radius

const view = ref<TimelineView | null>(null);
const error = ref("");
const flash = ref("");
const onlyFlagged = ref(false);
const wheelMode = ref(false);
const selected = ref("");
const saving = ref(false);
const dragRef = ref(""); // ref currently being dragged
const svgEl = ref<SVGSVGElement | null>(null);

// edit panel drafts
const edit = ref<{ title: string; objective: string; order: number; prereqs: string[] }>({
  title: "",
  objective: "",
  order: 0,
  prereqs: [],
});

interface Placed extends TimelineItem {
  lane: number;
  x: number;
  y: number;
  cx: number; // gem center x
  cy: number; // gem center y
  flagged: boolean;
}

const proj = () => currentProject();

const layout = computed(() => {
  const items = view.value?.items ?? [];
  const laneOf = (kind: string): number => (kind === "event" ? 0 : 1);
  const subCounters = new Map<string, number>();
  const subMax = [0, 0];
  const withSub = items.map((it) => {
    const lane = laneOf(it.kind);
    const key = `${lane}:${it.rank}`;
    const sub = subCounters.get(key) ?? 0;
    subCounters.set(key, sub + 1);
    subMax[lane] = Math.max(subMax[lane], sub);
    return { it, lane, sub };
  });

  const eventTop = 40;
  const eventBand = (subMax[0] + 1) * (BOX_H + ROW_GAP);
  const questTop = eventTop + eventBand + LANE_GAP;
  const questBand = (subMax[1] + 1) * (BOX_H + ROW_GAP);
  const axisY = questTop + questBand + 14;

  const placed: Placed[] = withSub.map(({ it, lane, sub }) => {
    const x = MARGIN_LEFT + it.rank * COL + COL / 2;
    const y = (lane === 0 ? eventTop : questTop) + sub * (BOX_H + ROW_GAP);
    return {
      ...it,
      lane,
      x,
      y,
      cx: x,
      cy: y + BOX_H / 2,
      flagged: it.flags.length > 0,
    };
  });

  return {
    placed,
    eventTop,
    questTop,
    axisY,
    width: MARGIN_LEFT + Math.max(view.value?.rank_count ?? 1, 1) * COL + 20,
    height: axisY + 26,
    laneLabelEvent: eventTop + BOX_H / 2,
    laneLabelQuest: questTop + BOX_H / 2,
  };
});

// 纪元轮盘：把星钻按 order 摆到同心环上（内环=最早）。早先一 rank 一环、order 一多就挤成一团；
// 现在把 ranks 分桶到至多 ~10 个间距充足的环，环内按角度均分（相邻环错开起始角防对齐），
// 轮盘整体随内容放大，外面再套缩放画布看细节。
const WHEEL_INNER_R = 76;
const WHEEL_RING_GAP = 48;
const WHEEL_MAX_RINGS = 10;
const wheelLayout = computed(() => {
  const items = view.value?.items ?? [];
  const ranks = [...new Set(items.map((it) => it.rank))].sort((a, b) => a - b);
  const ringCount = Math.min(ranks.length, WHEEL_MAX_RINGS) || 1;
  const rankToRing = new Map<number, number>();
  ranks.forEach((rk, idx) => {
    rankToRing.set(
      rk,
      ranks.length <= 1 ? 0 : Math.round((idx / (ranks.length - 1)) * (ringCount - 1)),
    );
  });
  const outerR = WHEEL_INNER_R + (ringCount - 1) * WHEEL_RING_GAP;
  const pad = GEM_R * 2 + 48;
  const size = (outerR + pad) * 2;
  const cx = size / 2;
  const cy = size / 2;
  const perRing = new Map<number, TimelineItem[]>();
  for (const it of items) {
    const ring = rankToRing.get(it.rank) ?? 0;
    const arr = perRing.get(ring);
    if (arr) arr.push(it);
    else perRing.set(ring, [it]);
  }
  const placed: Placed[] = [];
  for (const [ring, arr] of perRing) {
    const ringR = WHEEL_INNER_R + ring * WHEEL_RING_GAP;
    const startAngle = -Math.PI / 2 + ring * 0.55;
    arr.forEach((it, i) => {
      const angle = startAngle + (i / arr.length) * Math.PI * 2;
      placed.push({
        ...it,
        lane: it.kind === "event" ? 0 : 1,
        x: cx + ringR * Math.cos(angle),
        y: cy + ringR * Math.sin(angle),
        cx: cx + ringR * Math.cos(angle),
        cy: cy + ringR * Math.sin(angle),
        flagged: it.flags.length > 0,
      });
    });
  }
  const rings = Array.from({ length: ringCount }, (_, i) => WHEEL_INNER_R + i * WHEEL_RING_GAP);
  return { placed, cx, cy, rings, size };
});

const activePlaced = computed(() => (wheelMode.value ? wheelLayout.value.placed : layout.value.placed));

const posByRef = computed(() => new Map(activePlaced.value.map((p) => [p.ref, p])));

const edges = computed(() =>
  (view.value?.dependencies ?? [])
    .map((dep) => {
      const s = posByRef.value.get(dep.source);
      const t = posByRef.value.get(dep.target);
      if (!s || !t) return null;
      return { dep, s, t };
    })
    .filter((e): e is { dep: TimelineDependency; s: Placed; t: Placed } => e !== null),
);

const flaggedItems = computed(() => layout.value.placed.filter((p) => p.flagged));

const ticks = computed(() =>
  Array.from({ length: view.value?.rank_count ?? 0 }, (_, i) => ({
    n: i + 1,
    x: MARGIN_LEFT + i * COL + COL / 2,
  })),
);

// distinct (rank -> order) for the axis columns, so a drop x can map to a real order.
const rankOrders = computed(() => {
  const m = new Map<number, number>();
  for (const it of view.value?.items ?? []) {
    if (!m.has(it.rank)) m.set(it.rank, it.order);
  }
  return [...m.entries()].map(([rank, order]) => ({ rank, order, x: MARGIN_LEFT + rank * COL + COL / 2 }));
});

const maxOrder = computed(() => {
  let mx = 0;
  for (const it of view.value?.items ?? []) mx = Math.max(mx, it.order);
  return mx;
});

const selectedItem = computed(() => activePlaced.value.find((p) => p.ref === selected.value));
const selectedIsQuest = computed(() => selectedItem.value?.kind === "quest");
const selectedId = computed(() => selected.value.split(":")[1] ?? "");

// WS-E playtest: walk the quest's logic and show the path + outcome
interface SimRun {
  status: string;
  path: string[];
  message: string;
  final_state: Record<string, unknown>;
}
const SIM_LABEL: Record<string, string> = {
  completed: "可通关",
  blocked: "前置卡住",
  deadlock: "走不通（死锁）",
  cycle: "存在循环",
};
const sim = ref<SimRun | null>(null);
async function runSim(): Promise<void> {
  sim.value = null;
  try {
    const body = await apiPost<{ run: SimRun }>(
      `/projects/${proj()}/quests/${encodeURIComponent(selectedId.value)}:simulate`,
      {},
    );
    sim.value = body.run;
  } catch (e) {
    notifyError(e);
  }
}

// B7: AI-draft this quest's logic; the deterministic audit gates it, then it goes to review (HITL)
interface LogicDraft {
  logic_issues: { code: string; message: string }[];
  refine_trail: { round: number; verdict: string; blocking_count: number }[];
  auto_review_incomplete: boolean;
  review_item_id: string;
}
const intent = ref("");
const drafting = ref(false);
const draft = ref<LogicDraft | null>(null);
async function draftLogic(): Promise<void> {
  drafting.value = true;
  draft.value = null;
  try {
    draft.value = await apiPost<LogicDraft>(
      `/projects/${proj()}/quests/${encodeURIComponent(selectedId.value)}/logic:draft`,
      { intent: intent.value, llm_mode: "real" },
    );
  } catch (e) {
    notifyError(e);
  } finally {
    drafting.value = false;
  }
}

// WS-B collaboration: assignment + comment thread on the selected quest
interface Comment {
  id: string;
  author: string;
  body: string;
  at: string;
}
const collab = ref<{ assignee: string | null; comments: Comment[] }>({ assignee: null, comments: [] });
const draftComment = ref("");
function meName(): string {
  return (localStorage.getItem("owcopilot_operator") ?? "").trim();
}
async function loadCollab(): Promise<void> {
  collab.value = { assignee: null, comments: [] };
  try {
    const body = await apiGet<{ assignment: { assignee: string } | null; comments: Comment[] }>(
      `/projects/${proj()}/collab?object_ref=${encodeURIComponent(selected.value)}`,
    );
    collab.value = { assignee: body.assignment?.assignee ?? null, comments: body.comments };
  } catch (e) {
    notifyError(e);
  }
}
async function assignToMe(clear = false): Promise<void> {
  if (!meName()) {
    notifyError(new Error("请先在审阅台填写署名"));
    return;
  }
  try {
    await apiPost(`/projects/${proj()}/collab/assign`, {
      object_ref: selected.value,
      assignee: clear ? "" : meName(),
      by: meName(),
    });
    await loadCollab();
  } catch (e) {
    notifyError(e);
  }
}
async function postComment(): Promise<void> {
  if (!meName()) {
    notifyError(new Error("请先在审阅台填写署名"));
    return;
  }
  if (!draftComment.value.trim()) return;
  try {
    await apiPost(`/projects/${proj()}/collab/comments`, {
      object_ref: selected.value,
      author: meName(),
      body: draftComment.value.trim(),
    });
    draftComment.value = "";
    await loadCollab();
  } catch (e) {
    notifyError(e);
  }
}

// other quests, for the prerequisites multi-select
const otherQuests = computed(() =>
  (view.value?.items ?? [])
    .filter((it) => it.kind === "quest" && it.ref !== selected.value)
    .map((it) => ({ ref: it.ref, id: it.ref.split(":")[1] ?? "", label: it.label })),
);

function edgePath(s: Placed, t: Placed): string {
  if (wheelMode.value) {
    return `M${s.cx},${s.cy} L${t.cx},${t.cy}`;
  }
  // dip below the quest box and rise into the target — a readable arc even left-to-right
  const y = Math.max(s.cy, t.cy) + BOX_H / 2 + 14;
  return `M${s.cx},${s.cy + BOX_H / 2} C${s.cx},${y} ${t.cx},${y} ${t.cx},${t.cy + BOX_H / 2}`;
}

function gemColor(kind: string): string {
  return kind === "event" ? "#e0a878" : "#b89cf0";
}

// faceted diamond path centered at (cx,cy)
function gemPath(cx: number, cy: number, r: number): string {
  return `M${cx},${cy - r} L${cx + r * 0.74},${cy} L${cx},${cy + r} L${cx - r * 0.74},${cy} Z`;
}

function onSelect(p: Placed): void {
  selected.value = p.ref;
  if (p.kind === "quest") {
    const id = p.ref.split(":")[1] ?? "";
    // current prerequisites are inferred from incoming dependency edges (source -> this).
    // only quest sources belong in the quest `prerequisites` field — drop event deps.
    const prereqs = (view.value?.dependencies ?? [])
      .filter((d) => d.target === p.ref && d.source.startsWith("quest:"))
      .map((d) => d.source.split(":")[1] ?? "")
      .filter(Boolean);
    edit.value = { title: p.label, objective: "", order: p.order, prereqs };
    void hydrateQuest(id);
    sim.value = null;
    void loadCollab();
  } else {
    edit.value = { title: p.label, objective: "", order: p.order, prereqs: [] };
  }
}

// pull the quest's objective (not present in the timeline payload) for a faithful editor
async function hydrateQuest(id: string): Promise<void> {
  try {
    const body = await apiGet<{ quest: { title?: string; objective?: string } }>(
      `/projects/${proj()}/quests/${encodeURIComponent(id)}`,
    );
    if (selectedId.value === id) {
      if (typeof body.quest?.objective === "string") edit.value.objective = body.quest.objective;
      if (typeof body.quest?.title === "string") edit.value.title = body.quest.title;
    }
  } catch {
    // objective is optional polish; the order/title editor still works without it
  }
}

async function refresh(): Promise<void> {
  const body = await apiGet<{ timeline: TimelineView }>(`/projects/${proj()}/timeline`);
  view.value = body.timeline;
  // keep selection if the ref still exists
  if (selected.value && !view.value.items.some((it) => it.ref === selected.value)) {
    selected.value = "";
  }
}

async function patchQuestOrder(id: string, order: number | null): Promise<void> {
  await apiPatch(`/projects/${proj()}/quests/${encodeURIComponent(id)}`, {
    timeline_order: order,
    set_timeline_order: true,
  });
}

async function patchEventOrder(id: string, order: number | null): Promise<void> {
  await apiPatch(`/projects/${proj()}/entities/${encodeURIComponent(id)}`, {
    metadata_updates: { timeline_order: order },
  });
}

async function saveEdit(): Promise<void> {
  const item = selectedItem.value;
  if (!item) return;
  saving.value = true;
  error.value = "";
  flash.value = "";
  try {
    if (item.kind === "quest") {
      const body: Record<string, unknown> = {
        title: edit.value.title,
        objective: edit.value.objective,
        timeline_order: Number(edit.value.order),
        set_timeline_order: true,
        prerequisites: edit.value.prereqs,
      };
      await apiPatch(`/projects/${proj()}/quests/${encodeURIComponent(selectedId.value)}`, body);
    } else {
      await apiPatch(`/projects/${proj()}/entities/${encodeURIComponent(selectedId.value)}`, {
        name: edit.value.title,
        metadata_updates: { timeline_order: Number(edit.value.order) },
      });
    }
    flash.value = "已保存。";
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    saving.value = false;
  }
}

function togglePrereq(id: string): void {
  const idx = edit.value.prereqs.indexOf(id);
  if (idx >= 0) edit.value.prereqs.splice(idx, 1);
  else edit.value.prereqs.push(id);
}

// ---- best-effort horizontal drag: drop a gem onto the nearest rank column,
// adopt that column's order. The 顺序 number field stays the reliable path.
function clientToSvgX(clientX: number, clientY: number): number {
  const svg = svgEl.value;
  if (!svg) return 0;
  const ctm = svg.getScreenCTM();
  if (!ctm) return 0;
  const pt = svg.createSVGPoint();
  pt.x = clientX;
  pt.y = clientY;
  const local = pt.matrixTransform(ctm.inverse());
  return local.x;
}

function onPointerDown(p: Placed, e: PointerEvent): void {
  // don't let an enclosing ZoomCanvas read a node grab as a background pan
  e.stopPropagation();
  if (wheelMode.value) {
    onSelect(p);
    return; // drag only on the horizontal track
  }
  onSelect(p);
  dragRef.value = p.ref;
  (e.target as Element).setPointerCapture?.(e.pointerId);
}

const dragGhostX = ref<number | null>(null);
function onPointerMove(e: PointerEvent): void {
  if (!dragRef.value) return;
  dragGhostX.value = clientToSvgX(e.clientX, e.clientY);
}

async function onPointerUp(e: PointerEvent): Promise<void> {
  if (!dragRef.value) return;
  const draggedRef = dragRef.value;
  const item = view.value?.items.find((it) => it.ref === draggedRef);
  dragRef.value = "";
  dragGhostX.value = null;
  if (!item) return;
  const x = clientToSvgX(e.clientX, e.clientY);
  // nearest rank column by x
  let best = rankOrders.value[0];
  for (const c of rankOrders.value) {
    if (!best || Math.abs(c.x - x) < Math.abs(best.x - x)) best = c;
  }
  if (!best || best.order === item.order) return; // dropped on its own column — no-op
  saving.value = true;
  error.value = "";
  flash.value = "";
  try {
    if (item.kind === "quest") await patchQuestOrder(item.ref.split(":")[1] ?? "", best.order);
    else await patchEventOrder(item.ref.split(":")[1] ?? "", best.order);
    flash.value = "已按拖放更新顺序。";
    await refresh();
  } catch (err) {
    error.value = humanizeError(err);
  } finally {
    saving.value = false;
  }
}

async function sequenceIn(entry: TimelineEntry): Promise<void> {
  const next = maxOrder.value + 1;
  saving.value = true;
  error.value = "";
  flash.value = "";
  try {
    if (entry.kind === "quest") await patchQuestOrder(entry.ref.split(":")[1] ?? "", next);
    else await patchEventOrder(entry.ref.split(":")[1] ?? "", next);
    flash.value = "已排入时间线。";
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    saving.value = false;
  }
}

onMounted(async () => {
  try {
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  }
});
</script>

<template>
  <section>
    <PageHead overline="TIMELINE" title="时间线" purpose="任务与事件按编年排布，点星钻改序，编年违例标红。" />

    <p v-if="error" class="error">{{ error }}</p>
    <p v-if="flash" class="flash">{{ flash }}</p>

    <template v-if="view">
      <div v-if="!view.items.length && !view.unsequenced.length" class="pane empty">
        <p class="muted">还没有带时序的任务或事件。给任务填上时间顺序，这条轴就会长出来。</p>
      </div>

      <template v-else>
        <div class="toolbar">
          <label v-if="flaggedItems.length" class="check">
            <input v-model="onlyFlagged" type="checkbox" />
            <span>只高亮有问题的条目</span>
          </label>
          <label class="check">
            <input v-model="wheelMode" type="checkbox" />
            <span>纪元轮盘</span>
          </label>
          <span v-if="flaggedItems.length" class="warn-count">编年问题 <b>{{ flaggedItems.length }}</b></span>
          <span class="muted small spacer">点星钻打开右侧编辑面板</span>
        </div>

        <div class="layout">
          <div v-if="view.items.length" class="scroll pane" :class="{ saving }">
           <ZoomCanvas natural>
            <!-- HORIZONTAL TRACK (default) -->
            <svg
              v-if="!wheelMode"
              ref="svgEl"
              :viewBox="`0 0 ${layout.width} ${layout.height}`"
              :width="layout.width"
              class="tl"
              @pointermove="onPointerMove"
              @pointerup="onPointerUp"
              @pointercancel="onPointerUp"
            >
              <defs>
                <marker id="tl-ah" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
                  <path d="M0,0 L6,3 L0,6 Z" fill="#8fd6e8" />
                </marker>
                <marker id="tl-ahr" markerWidth="7" markerHeight="7" refX="6" refY="3" orient="auto">
                  <path d="M0,0 L6,3 L0,6 Z" fill="#e0705a" />
                </marker>
                <radialGradient id="tl-halo" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stop-color="#fff" stop-opacity="0.5" />
                  <stop offset="55%" stop-color="#fff" stop-opacity="0.12" />
                  <stop offset="100%" stop-color="#fff" stop-opacity="0" />
                </radialGradient>
                <!-- the world-line rail: a star-rail gradient with a light running along it -->
                <linearGradient id="tl-rail" x1="0" y1="0" x2="1" y2="0">
                  <stop offset="0%" stop-color="#8fd6e8" />
                  <stop offset="50%" stop-color="#f0d28a" />
                  <stop offset="100%" stop-color="#b9a7ff" />
                </linearGradient>
              </defs>

              <text x="6" :y="layout.laneLabelEvent" class="lane">事件</text>
              <text x="6" :y="layout.laneLabelQuest" class="lane">任务</text>

              <line x1="0" :y1="layout.axisY" :x2="layout.width" :y2="layout.axisY" class="axis" />
              <line x1="0" :y1="layout.axisY" :x2="layout.width" :y2="layout.axisY" class="rail-flow" />
              <text v-for="tk in ticks" :key="tk.n" :x="tk.x" :y="layout.axisY + 16" class="tick">
                {{ tk.n }}
              </text>

              <g class="edges">
                <path
                  v-for="(e, i) in edges"
                  :key="i"
                  :d="edgePath(e.s, e.t)"
                  class="edge"
                  :class="{ bad: e.dep.violation }"
                  :marker-end="e.dep.violation ? 'url(#tl-ahr)' : 'url(#tl-ah)'"
                />
              </g>

              <!-- drop hint: a soft vertical guide following the dragged gem -->
              <line
                v-if="dragRef && dragGhostX !== null"
                :x1="dragGhostX"
                y1="20"
                :x2="dragGhostX"
                :y2="layout.axisY"
                class="drop-guide"
              />

              <g
                v-for="p in layout.placed"
                :key="p.ref"
                class="node"
                :class="{
                  dim: onlyFlagged && !p.flagged,
                  sel: p.ref === selected,
                  dragging: p.ref === dragRef,
                }"
                @pointerdown="onPointerDown(p, $event)"
              >
                <!-- selection halo -->
                <circle
                  v-if="p.ref === selected"
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R * 2.4"
                  fill="url(#tl-halo)"
                  class="halo"
                />
                <!-- minimalist sci-fi station: thin ring + bright core (no fat gem) -->
                <circle
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R"
                  fill="rgba(10,14,36,0.55)"
                  :stroke="p.flagged ? '#e0705a' : gemColor(p.kind)"
                  :stroke-width="p.flagged ? 1.8 : 1.3"
                  class="station-ring"
                />
                <circle
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R * 0.3"
                  :fill="p.flagged ? '#e0705a' : gemColor(p.kind)"
                  class="station-core"
                />
                <!-- selection dashed ring -->
                <circle
                  v-if="p.ref === selected"
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R + 5"
                  class="sel-ring"
                />
                <text :x="p.cx" :y="p.cy + GEM_R + 13" class="label">{{ p.label }}</text>
                <text v-if="p.flagged" :x="p.cx + GEM_R + 2" :y="p.cy - GEM_R + 2" class="flag">!</text>
              </g>
            </svg>

            <!-- EPOCH WHEEL (optional concentric rings; horizontal track is default) -->
            <svg
              v-else
              :viewBox="`0 0 ${wheelLayout.size} ${wheelLayout.size}`"
              :width="wheelLayout.size"
              class="tl wheel"
            >
              <defs>
                <radialGradient id="tl-halo-w" cx="50%" cy="50%" r="50%">
                  <stop offset="0%" stop-color="#fff" stop-opacity="0.5" />
                  <stop offset="55%" stop-color="#fff" stop-opacity="0.12" />
                  <stop offset="100%" stop-color="#fff" stop-opacity="0" />
                </radialGradient>
              </defs>
              <circle
                v-for="(r, i) in wheelLayout.rings"
                :key="i"
                :cx="wheelLayout.cx"
                :cy="wheelLayout.cy"
                :r="r"
                class="ring"
              />
              <g class="edges">
                <path v-for="(e, i) in edges" :key="i" :d="edgePath(e.s, e.t)" class="edge" :class="{ bad: e.dep.violation }" />
              </g>
              <g
                v-for="p in wheelLayout.placed"
                :key="p.ref"
                class="node"
                :class="{ dim: onlyFlagged && !p.flagged, sel: p.ref === selected }"
                @pointerdown="onPointerDown(p, $event)"
              >
                <circle v-if="p.ref === selected" :cx="p.cx" :cy="p.cy" :r="GEM_R * 2.4" fill="url(#tl-halo-w)" class="halo" />
                <circle
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R"
                  fill="rgba(10,14,36,0.55)"
                  :stroke="p.flagged ? '#e0705a' : gemColor(p.kind)"
                  :stroke-width="p.flagged ? 1.8 : 1.3"
                  class="station-ring"
                />
                <circle
                  :cx="p.cx"
                  :cy="p.cy"
                  :r="GEM_R * 0.3"
                  :fill="p.flagged ? '#e0705a' : gemColor(p.kind)"
                  class="station-core"
                />
                <circle v-if="p.ref === selected" :cx="p.cx" :cy="p.cy" :r="GEM_R + 5" class="sel-ring" />
                <text :x="p.cx" :y="p.cy + GEM_R + 13" class="label">{{ p.label }}</text>
                <text v-if="p.flagged" :x="p.cx + GEM_R + 2" :y="p.cy - GEM_R + 2" class="flag">!</text>
              </g>
            </svg>
           </ZoomCanvas>
          </div>

          <!-- SELECTION / EDIT PANEL -->
          <div v-if="selectedItem" class="pane detail">
            <div class="d-head">
              <svg class="d-gem-svg" viewBox="0 0 16 16" width="16" height="16">
                <path :d="gemPath(8, 8, 6)" :fill="gemColor(selectedItem.kind)" fill-opacity="0.3" :stroke="gemColor(selectedItem.kind)" stroke-width="1.2" />
              </svg>
              <b>{{ selectedItem.label }}</b>
              <span class="muted">{{ selectedItem.kind === "event" ? "事件" : "任务" }}</span>
            </div>

            <label class="f">
              <span>{{ selectedIsQuest ? "标题" : "名称" }}</span>
              <input v-model="edit.title" maxlength="160" />
            </label>

            <label v-if="selectedIsQuest" class="f">
              <span>目标</span>
              <textarea v-model="edit.objective" rows="3"></textarea>
            </label>

            <label class="f narrow">
              <span>时间顺序</span>
              <input v-model.number="edit.order" type="number" step="1" />
            </label>

            <template v-if="selectedIsQuest">
              <div class="section sub"><span class="t">前置任务</span></div>
              <p v-if="!otherQuests.length" class="muted small">还没有其它任务可作前置。</p>
              <div v-else class="prereqs">
                <label v-for="q in otherQuests" :key="q.id" class="pq">
                  <input
                    type="checkbox"
                    :checked="edit.prereqs.includes(q.id)"
                    @change="togglePrereq(q.id)"
                  />
                  <span>{{ q.label }}</span>
                </label>
              </div>
            </template>

            <div class="d-actions">
              <button class="primary" :disabled="saving" @click="saveEdit">保存</button>
              <button v-if="selectedIsQuest" class="ghost" @click="runSim">试玩</button>
              <button class="ghost" @click="selected = ''">关闭</button>
            </div>

            <div v-if="sim && selectedIsQuest" class="sim">
              <span class="sim-status" :class="sim.status">{{ SIM_LABEL[sim.status] ?? sim.status }}</span>
              <span class="sim-path mono">{{ sim.path.join(" → ") || "（无阶段）" }}</span>
              <span v-if="sim.message" class="muted sim-msg">{{ sim.message }}</span>
            </div>

            <div v-if="selectedIsQuest" class="b7">
              <div class="section sub"><span class="t">AI 起草任务逻辑</span></div>
              <p class="muted small">
                让模型起草变量/分支/前置，确定性审计当场校验后送审，人审通过才落地。
              </p>
              <input
                v-model="intent"
                class="b7-intent"
                placeholder="设计意图（可选）：如「先见长老才能进峡谷；带护身符避免受伤结局」"
              />
              <button class="ghost" :disabled="drafting" @click="draftLogic">
                {{ drafting ? "起草中…" : "AI 起草逻辑" }}
              </button>
              <div v-if="draft" class="b7-out">
                <div class="b7-trail">
                  <span v-for="s in draft.refine_trail" :key="s.round" class="b7-round" :class="s.verdict">
                    第{{ s.round + 1 }}轮 {{ s.verdict === "pass" ? "审计通过" : `修正${s.blocking_count}项` }}
                  </span>
                </div>
                <p v-if="!draft.logic_issues.length" class="b7-clean">
                  逻辑审计通过，已送审阅台等人审落定。
                </p>
                <ul v-else class="b7-issues">
                  <li v-for="(i, k) in draft.logic_issues" :key="k">{{ i.code }}：{{ i.message }}</li>
                </ul>
                <p v-if="draft.auto_review_incomplete" class="muted small">
                  模型多次输出难以收敛，已标记请人工重点复核。
                </p>
              </div>
            </div>

            <div v-if="selectedIsQuest" class="collab">
              <div class="collab-assign">
                <span class="muted">负责人：</span>
                <span v-if="collab.assignee" class="assignee">@{{ collab.assignee }}</span>
                <span v-else class="muted">未指派</span>
                <button class="ghost xs" @click="assignToMe(false)">指派给我</button>
                <button v-if="collab.assignee" class="ghost xs" @click="assignToMe(true)">取消</button>
              </div>
              <div v-for="c in collab.comments" :key="c.id" class="cmt">
                <span class="cmt-author">{{ c.author }}</span>
                <span class="cmt-body">{{ c.body }}</span>
              </div>
              <div class="collab-add">
                <input v-model="draftComment" placeholder="留个评论…" @keydown.enter="postComment" />
                <button class="ghost xs" @click="postComment">评论</button>
              </div>
            </div>
          </div>
        </div>

        <ul v-if="flaggedItems.length" class="issues">
          <li v-for="p in flaggedItems" :key="p.ref" class="issue pane">
            <div class="ih">
              <span class="ik">{{ p.kind === "event" ? "事件" : "任务" }}</span>
              <span class="iname">{{ p.label }}</span>
              <span class="iref muted">{{ p.ref }}</span>
            </div>
            <div class="ireasons">
              <span v-for="f in p.flags" :key="f" class="reason">{{ f }}</span>
            </div>
          </li>
        </ul>

        <div v-if="view.unsequenced.length" class="unseq">
          <div class="section sub"><span class="t">未定序（缺时间顺序）</span></div>
          <p class="muted small">这些还没排进时间线，点「排入」会接到当前最末位，纳入校验后可再微调。</p>
          <div class="chips">
            <span v-for="u in view.unsequenced" :key="u.ref" class="chip">
              <i>{{ u.kind === "event" ? "事件" : "任务" }}</i> {{ u.label }}
              <button class="seq" :disabled="saving" @click="sequenceIn(u)">排入</button>
            </span>
          </div>
        </div>
      </template>
    </template>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.empty {
  padding: 1.4rem;
  text-align: center;
}

.toolbar {
  display: flex;
  align-items: center;
  gap: 0.9rem;
  margin: 0.2rem 0 0.6rem;
  flex-wrap: wrap;
}

.check {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.84rem;
  color: var(--ow-ink);
}

/* consistent count chip (matches the red pills on Overview / Impact) instead of a bare phrase */
.warn-count {
  border: 1px solid rgba(224, 133, 133, 0.5);
  background: rgba(224, 133, 133, 0.1);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: #e89a9a;
  font-size: 0.78rem;
  padding: 0.14rem 0.62rem;
}
.warn-count b {
  color: var(--ow-ink);
  font-variant-numeric: tabular-nums;
}

.spacer {
  margin-left: auto;
}

.layout {
  display: grid;
  grid-template-columns: 1fr 18rem;
  gap: 0.9rem;
  align-items: start;
}

.scroll {
  overflow-x: auto;
  padding: 0.8rem;
  transition: opacity 0.15s ease;
}

.scroll.saving {
  opacity: 0.6;
}

.tl {
  display: block;
  height: auto;
  touch-action: none;
}

.tl.wheel {
  margin: 0 auto;
}

.lane {
  fill: var(--ow-muted);
  font-size: 11px;
}

/* world-line rail: a glowing star-rail gradient with a light running its length */
.axis {
  stroke: url(#tl-rail);
  stroke-width: 2.5;
  stroke-linecap: round;
  filter: drop-shadow(0 0 5px rgba(240, 210, 138, 0.45));
}
.rail-flow {
  stroke: #fff6d6;
  stroke-width: 2.5;
  stroke-linecap: round;
  stroke-dasharray: 18 64;
  opacity: 0.55;
  animation: tl-rail-flow 3.2s linear infinite;
}
@keyframes tl-rail-flow {
  to {
    stroke-dashoffset: -82;
  }
}
@media (prefers-reduced-motion: reduce) {
  .rail-flow {
    animation: none;
    display: none;
  }
}

.ring {
  fill: none;
  stroke: var(--ow-line);
  stroke-opacity: 0.5;
  stroke-dasharray: 2 4;
}

.tick {
  fill: #6b6a82;
  font-size: 9px;
  text-anchor: middle;
}

.edge {
  fill: none;
  stroke: #5a6a9a;
  stroke-width: 1.3;
}

.edge.bad {
  stroke: #e0705a;
  stroke-dasharray: 4 3;
}

.drop-guide {
  stroke: var(--ow-gold-soft);
  stroke-width: 1;
  stroke-dasharray: 3 4;
  opacity: 0.7;
}

.node {
  transition: opacity 0.15s ease;
  cursor: pointer;
}

.node.dim {
  opacity: 0.32;
}

.node.dragging {
  opacity: 0.55;
}

.gem {
  transition:
    fill-opacity 0.15s ease,
    stroke-width 0.15s ease;
}

.node:hover .gem {
  fill-opacity: 0.36;
}

/* minimalist sci-fi station marker (ring + glowing core) */
.station-ring {
  transition: stroke-width 0.15s ease;
}
.node:hover .station-ring {
  stroke-width: 2;
}
.station-core {
  filter: drop-shadow(0 0 3px rgba(240, 210, 138, 0.55));
}

.glow {
  pointer-events: none;
  opacity: 0.35;
}

.node.sel .glow {
  opacity: 0.6;
}

.halo {
  pointer-events: none;
}

.sel-ring {
  fill: none;
  stroke: var(--ow-gold-bright);
  stroke-width: 1.4;
  stroke-dasharray: 3 3;
  pointer-events: none;
  transform-box: fill-box;
  transform-origin: center;
  animation: tl-spin 6s linear infinite;
}

@keyframes tl-spin {
  to {
    transform: rotate(360deg);
  }
}

.label {
  fill: var(--ow-ink);
  font-size: 11px;
  text-anchor: middle;
  font-family: var(--ow-serif);
  pointer-events: none;
}

.flag {
  fill: #e0705a;
  font-size: 12px;
  text-anchor: middle;
  pointer-events: none;
}

/* edit panel — mirrors GraphPage's detail pane */
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
.d-gem-svg {
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
.f.narrow input {
  max-width: 8rem;
}
input,
textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.86rem;
}
input:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}
.prereqs {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  max-height: 12rem;
  overflow-y: auto;
}
.pq {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.82rem;
  color: var(--ow-ink);
}
.d-actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.3rem;
}
.b7 {
  margin-top: 0.7rem;
  border-top: 1px solid var(--ow-line);
  padding-top: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}
.b7 .small {
  font-size: 0.78rem;
  margin: 0;
}
.b7-intent {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.4rem 0.6rem;
  font: inherit;
  font-size: 0.82rem;
}
.b7-trail {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}
.b7-round {
  font-size: 0.74rem;
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.05rem 0.5rem;
  color: var(--ow-muted);
}
.b7-round.pass {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
}
.b7-clean {
  color: var(--ow-gold-bright);
  font-size: 0.82rem;
  margin: 0.3rem 0 0;
}
.b7-issues {
  margin: 0.3rem 0 0;
  padding-left: 1.1rem;
  color: #e89a9a;
  font-size: 0.8rem;
}
.sim {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-top: 0.6rem;
  padding-top: 0.5rem;
  border-top: 1px solid var(--ow-line);
}
.sim-status {
  font-size: 0.74rem;
  border: 1px solid var(--ow-line);
  border-radius: 0.3rem;
  padding: 0.1rem 0.45rem;
}
.sim-status.completed {
  color: #6fcf97;
  border-color: #6fcf97;
}
.sim-status.deadlock,
.sim-status.blocked,
.sim-status.cycle {
  color: var(--ow-flag, #e0653a);
  border-color: var(--ow-flag, #e0653a);
}
.sim-path {
  font-size: 0.78rem;
  color: var(--ow-ink-dim);
}
.sim-msg {
  font-size: 0.74rem;
}
.collab {
  margin-top: 0.6rem;
  padding-top: 0.5rem;
  border-top: 1px solid var(--ow-line);
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
}
.collab-assign {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.78rem;
}
.collab-assign .assignee {
  color: var(--ow-gold, #d8b46a);
}
.collab .xs {
  font-size: 0.7rem;
  padding: 0.1rem 0.4rem;
}
.cmt {
  display: flex;
  gap: 0.4rem;
  font-size: 0.78rem;
}
.cmt-author {
  color: var(--ow-ink-dim);
  white-space: nowrap;
}
.collab-add {
  display: flex;
  gap: 0.4rem;
}
.collab-add input {
  flex: 1;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  color: var(--ow-ink);
  padding: 0.3rem 0.5rem;
  font-size: 0.78rem;
}

button {
  border-radius: var(--ow-control-radius);
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
button:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.issues {
  list-style: none;
  margin: 0.8rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.issue {
  padding: 0.55rem 0.8rem;
}

.ih {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.ik {
  border: 1px solid rgba(224, 112, 90, 0.45);
  background: rgba(224, 112, 90, 0.1);
  color: #e0a878;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  font-size: 0.72rem;
  padding: 0.04rem 0.5rem;
}

.iname {
  font-family: var(--ow-serif);
  color: var(--ow-ink);
  font-size: 0.92rem;
}

.iref {
  font-size: 0.72rem;
  font-family: ui-monospace, Consolas, monospace;
}

.ireasons {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-top: 0.35rem;
}

.reason {
  border: 1px solid rgba(224, 112, 90, 0.4);
  background: rgba(224, 112, 90, 0.08);
  color: #e6a98f;
  border-radius: 0.4rem;
  font-size: 0.76rem;
  padding: 0.08rem 0.45rem;
}

.unseq {
  margin-top: 1.1rem;
}

.sub {
  margin-bottom: 0.3rem;
}

.small {
  font-size: 0.78rem;
  margin-top: 0;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-top: 0.4rem;
}

.chip {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  background: rgba(16, 22, 48, 0.6);
  color: var(--ow-ink);
  font-size: 0.78rem;
  padding: 0.16rem 0.45rem 0.16rem 0.65rem;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
}

.chip i {
  color: var(--ow-muted);
  font-style: normal;
  margin-right: 0.1rem;
}

.chip .seq {
  padding: 0.06rem 0.5rem;
  font-size: 0.74rem;
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-faint);
  background: transparent;
}

.error {
  color: #e89a9a;
}
.flash {
  color: #8ed4ac;
}

@media (max-width: 820px) {
  .layout {
    grid-template-columns: 1fr;
  }
}

@media (prefers-reduced-motion: reduce) {
  .node,
  .gem,
  .scroll {
    transition: none;
  }
  .sel-ring {
    animation: none;
  }
}
</style>
