<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  ApiError,
  humanizeError,
  apiGet,
  apiPatch,
  apiPost,
  currentProject,
  llmParams,
} from "../api";
import { notifyError } from "../toast";
import PageHead from "../components/PageHead.vue";
import AsyncState from "../components/AsyncState.vue";
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
const initialLoading = ref(true);
const flash = ref("");
const onlyFlagged = ref(false);
const wheelMode = ref(false);
const selected = ref("");
const route = useRoute();
const saving = ref(false);
const dragRef = ref(""); // ref currently being dragged
const svgEl = ref<SVGSVGElement | null>(null);

// edit panel drafts
interface TimelineEditDraft {
  title: string;
  objective: string;
  giverNpc: string;
  location: string;
  order: number;
  prereqs: string[];
}

const edit = ref<TimelineEditDraft>({
  title: "",
  objective: "",
  giverNpc: "",
  location: "",
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
const questEtag = ref("");
const hydratingQuest = ref(false);
const hydrateError = ref("");
let selectionEpoch = 0;

// WS-E playtest: walk the quest's logic and show the path + outcome
interface SimRun {
  status: string;
  path: string[];
  steps: { stage_id: string; precondition_ok: boolean; effects: string[]; branch_taken: string }[];
  message: string;
  final_state: Record<string, unknown>;
}
interface DialogueWalk {
  tree_id: string;
  status: string;
  path: string[];
  steps: { node_id: string; chosen: string; effects: string[] }[];
  message: string;
  final_state: Record<string, unknown>;
}
interface PlaytestResult {
  quest_id: string;
  run: SimRun;
  dialogue_walk?: DialogueWalk;
}
const SIM_LABEL: Record<string, string> = {
  completed: "可通关",
  unavailable: "任务尚不可接取",
  blocked: "前置卡住",
  deadlock: "走不通（死锁）",
  cycle: "存在循环",
  dangling: "存在悬空节点",
};
const sim = ref<PlaytestResult | null>(null);
const simBusy = ref(false);
const simError = ref("");
const questChoicesText = ref("");
const initialStateText = ref("{}");
const dialogueTreeId = ref("");
const dialogueChoicesText = ref("");
let simRequestSerial = 0;

function selectorList(source: string): string[] {
  return source.split(/[\s,，]+/).map((item) => item.trim()).filter(Boolean);
}

function parseInitialState(source: string): Record<string, boolean | number | string> {
  const parsed: unknown = JSON.parse(source.trim() || "{}");
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("初始变量必须是 JSON 对象，例如 {\"has_key\": true}。");
  }
  const state: Record<string, boolean | number | string> = {};
  for (const [key, value] of Object.entries(parsed)) {
    if (!key.trim()) throw new Error("初始变量名不能为空。");
    if (
      typeof value !== "boolean" &&
      typeof value !== "string" &&
      !(typeof value === "number" && Number.isInteger(value))
    ) {
      throw new Error(`变量「${key}」只支持布尔值、整数或字符串。`);
    }
    state[key] = value;
  }
  return state;
}

function formatState(state: Record<string, unknown>): string {
  return JSON.stringify(state, null, 2);
}

async function runSim(): Promise<void> {
  const objectRef = selected.value;
  const id = selectedId.value;
  const epoch = selectionEpoch;
  const requestSerial = ++simRequestSerial;
  if (!objectRef.startsWith("quest:") || !id) return;
  let initialState: Record<string, boolean | number | string>;
  try {
    initialState = parseInitialState(initialStateText.value);
  } catch (error) {
    simError.value = humanizeError(error);
    return;
  }
  const dialogueId = dialogueTreeId.value.trim();
  const payload: Record<string, unknown> = {
    choices: selectorList(questChoicesText.value),
    initial_state: initialState,
  };
  if (dialogueId) {
    payload.dialogue_tree_id = dialogueId;
    payload.dialogue_choices = selectorList(dialogueChoicesText.value);
  }
  sim.value = null;
  simError.value = "";
  simBusy.value = true;
  try {
    const body = await apiPost<PlaytestResult>(
      `/projects/${proj()}/quests/${encodeURIComponent(id)}:simulate`,
      payload,
    );
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      simRequestSerial === requestSerial
    ) {
      sim.value = body;
    }
  } catch (e) {
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      simRequestSerial === requestSerial
    ) {
      simError.value = humanizeError(e);
      notifyError(e);
    }
  } finally {
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      simRequestSerial === requestSerial
    ) {
      simBusy.value = false;
    }
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
let draftRequestSerial = 0;
async function draftLogic(): Promise<void> {
  const objectRef = selected.value;
  const id = selectedId.value;
  const epoch = selectionEpoch;
  const requestSerial = ++draftRequestSerial;
  const intentSnapshot = intent.value;
  if (!objectRef.startsWith("quest:") || !id) return;
  drafting.value = true;
  draft.value = null;
  try {
    const response = await apiPost<LogicDraft>(
      `/projects/${proj()}/quests/${encodeURIComponent(id)}/logic:draft`,
      { intent: intentSnapshot, ...llmParams() },
    );
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      draftRequestSerial === requestSerial
    ) {
      draft.value = response;
    }
  } catch (e) {
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      draftRequestSerial === requestSerial
    ) {
      notifyError(e);
    }
  } finally {
    if (
      selected.value === objectRef &&
      selectionEpoch === epoch &&
      draftRequestSerial === requestSerial
    ) {
      drafting.value = false;
    }
  }
}

// WS-B collaboration: assignment + comment thread on the selected quest
interface Comment {
  id: string;
  author: string;
  body: string;
  at: string;
}
interface EditLock {
  holder: string;
  at: string;
}
interface CollabPageResponse {
  assignment: { assignee: string } | null;
  comments: Comment[];
  lock: EditLock | null;
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
}
const COLLAB_PAGE_SIZE = 20;
const collab = ref<{ assignee: string | null; comments: Comment[]; lock: EditLock | null }>({
  assignee: null,
  comments: [],
  lock: null,
});
const collabLoading = ref(false);
const collabLoadingMore = ref(false);
const collabTotal = ref(0);
const collabNextCursor = ref<string | null>(null);
const collabSnapshotEtag = ref("");
const draftComment = ref("");
const commentBusy = ref(false);
const lockBusy = ref(false);
let lockHeartbeatTimer = 0;
let lockBusyCount = 0;
const lockDesired = new Map<string, boolean>();
const lockRequestChains = new Map<string, Promise<void>>();
let collabRequestSerial = 0;

function markLockBusy(delta: 1 | -1): void {
  lockBusyCount = Math.max(0, lockBusyCount + delta);
  lockBusy.value = lockBusyCount > 0;
}

function queueLockRequest<T>(objectRef: string, request: () => Promise<T>): Promise<T> {
  const previous = lockRequestChains.get(objectRef) ?? Promise.resolve();
  const result = previous.catch(() => undefined).then(request);
  const settled = result.then(
    () => undefined,
    () => undefined,
  );
  lockRequestChains.set(objectRef, settled);
  void settled.then(() => {
    if (lockRequestChains.get(objectRef) === settled) lockRequestChains.delete(objectRef);
  });
  return result;
}

function meName(): string {
  return (localStorage.getItem("owcopilot_operator") ?? "").trim();
}
const lockHeldByMe = computed(() => Boolean(meName() && collab.value.lock?.holder === meName()));
const lockedByOther = computed(() => Boolean(collab.value.lock && !lockHeldByMe.value));

function resetCollabView(): void {
  collabRequestSerial += 1;
  collab.value = { assignee: null, comments: [], lock: null };
  collabLoading.value = false;
  collabLoadingMore.value = false;
  collabTotal.value = 0;
  collabNextCursor.value = null;
  collabSnapshotEtag.value = "";
  draftComment.value = "";
}

async function loadCollab(objectRef = selected.value, append = false): Promise<void> {
  if (!objectRef) {
    resetCollabView();
    return;
  }
  const cursor = append ? collabNextCursor.value : null;
  if (append && (!cursor || collabLoadingMore.value)) return;
  const requestSerial = ++collabRequestSerial;
  if (selected.value === objectRef) {
    if (append) collabLoadingMore.value = true;
    else {
      collab.value = { assignee: null, comments: [], lock: null };
      collabLoading.value = true;
      collabLoadingMore.value = false;
    }
  }
  try {
    const cursorQuery = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const body = await apiGet<CollabPageResponse>(
      `/projects/${proj()}/collab?object_ref=${encodeURIComponent(objectRef)}`
        + `&limit=${COLLAB_PAGE_SIZE}${cursorQuery}`,
    );
    if (selected.value === objectRef && collabRequestSerial === requestSerial) {
      collab.value = {
        assignee: body.assignment?.assignee ?? null,
        comments: append ? [...collab.value.comments, ...body.comments] : body.comments,
        lock: body.lock,
      };
      collabTotal.value = body.total;
      collabNextCursor.value = body.next_cursor;
      collabSnapshotEtag.value = body.snapshot_etag;
    }
  } catch (e) {
    if (selected.value === objectRef && collabRequestSerial === requestSerial) {
      if (append && e instanceof ApiError && e.status === 409) {
        collabLoadingMore.value = false;
        await loadCollab(objectRef);
        return;
      }
      notifyError(e);
    }
  } finally {
    if (selected.value === objectRef && collabRequestSerial === requestSerial) {
      if (append) collabLoadingMore.value = false;
      else collabLoading.value = false;
    }
  }
}

async function setEditLock(release: boolean, objectRef = selected.value, quiet = false): Promise<boolean> {
  const holder = meName();
  if (!holder) {
    if (!quiet) notifyError("请先在审阅台填写署名，再锁定编辑。");
    return false;
  }
  if (!objectRef) return false;
  // Mark intent before queueing. A release therefore prevents a queued heartbeat from
  // renewing the old object; any already in-flight renewal is followed by the release.
  lockDesired.set(objectRef, !release);
  markLockBusy(1);
  try {
    return await queueLockRequest(objectRef, async () => {
      if (!release && lockDesired.get(objectRef) !== true) return false;
      await apiPost(`/projects/${proj()}/collab/lock`, { object_ref: objectRef, holder, release });
      if (release && lockDesired.get(objectRef) === false) lockDesired.delete(objectRef);
      if (selected.value === objectRef) await loadCollab(objectRef);
      return true;
    });
  } catch (error) {
    if (!quiet) notifyError(error);
    return false;
  } finally {
    markLockBusy(-1);
  }
}

interface LockResult {
  ok: boolean;
  acquired: boolean;
}

async function ensureOwnLock(objectRef: string): Promise<LockResult> {
  if (!objectRef.startsWith("quest:")) return { ok: true, acquired: false };
  let believedMine = selected.value === objectRef && lockHeldByMe.value;
  if (selected.value === objectRef && lockedByOther.value) {
    await loadCollab(objectRef);
    if (lockedByOther.value) {
      notifyError(`“${collab.value.lock?.holder}”正在编辑这项任务，请稍后刷新。`);
      return { ok: false, acquired: false };
    }
    believedMine = lockHeldByMe.value;
  } else if (selected.value !== objectRef) {
    try {
      const state = await apiGet<{ lock: EditLock | null }>(
        `/projects/${proj()}/collab?object_ref=${encodeURIComponent(objectRef)}&limit=1`,
      );
      if (state.lock && state.lock.holder !== meName()) {
        notifyError(`“${state.lock.holder}”正在编辑这项任务，请稍后重试。`);
        return { ok: false, acquired: false };
      }
      believedMine = state.lock?.holder === meName();
    } catch (error) {
      notifyError(error);
      return { ok: false, acquired: false };
    }
  }
  const ok = await setEditLock(false, objectRef);
  return { ok, acquired: ok && !believedMine };
}

function syncLockHeartbeat(): void {
  window.clearInterval(lockHeartbeatTimer);
  lockHeartbeatTimer = 0;
  const objectRef = selected.value;
  if (!objectRef || !lockHeldByMe.value) return;
  // A release queued behind an acquire/heartbeat is authoritative. A late collab refresh may still
  // report the old lock briefly; it must not turn that explicit false intent back into a renewal.
  if (lockDesired.get(objectRef) === false) return;
  const holder = meName();
  lockDesired.set(objectRef, true);
  lockHeartbeatTimer = window.setInterval(() => {
    if (
      selected.value !== objectRef ||
      !lockHeldByMe.value ||
      meName() !== holder ||
      lockDesired.get(objectRef) !== true
    ) {
      return;
    }
    void queueLockRequest(objectRef, async () => {
      if (
        selected.value !== objectRef ||
        !lockHeldByMe.value ||
        meName() !== holder ||
        lockDesired.get(objectRef) !== true
      ) {
        return;
      }
      try {
        await apiPost(`/projects/${proj()}/collab/lock`, {
          object_ref: objectRef,
          holder,
          release: false,
        });
      } catch (error) {
        if (selected.value === objectRef && lockDesired.get(objectRef) === true) {
          notifyError(`编辑锁续期失败；保存前会重新校验锁。${humanizeError(error)}`);
          await loadCollab(objectRef);
        }
      }
    });
  }, 45_000);
}

watch(() => `${selected.value}|${lockHeldByMe.value}`, syncLockHeartbeat);
async function assignToMe(clear = false): Promise<void> {
  const objectRef = selected.value;
  const operator = meName();
  if (!operator) {
    notifyError(new Error("请先在审阅台填写署名"));
    return;
  }
  try {
    await apiPost(`/projects/${proj()}/collab/assign`, {
      object_ref: objectRef,
      assignee: clear ? "" : operator,
      by: operator,
    });
    if (selected.value === objectRef) await loadCollab(objectRef);
  } catch (e) {
    if (selected.value === objectRef) notifyError(e);
  }
}
async function postComment(): Promise<void> {
  const objectRef = selected.value;
  const author = meName();
  const body = draftComment.value.trim();
  if (!author) {
    notifyError(new Error("请先在审阅台填写署名"));
    return;
  }
  if (!objectRef || !body || commentBusy.value) return;
  commentBusy.value = true;
  try {
    await apiPost(`/projects/${proj()}/collab/comments`, {
      object_ref: objectRef,
      author,
      body,
    });
    if (selected.value === objectRef) {
      if (draftComment.value.trim() === body) draftComment.value = "";
      await loadCollab(objectRef);
    }
  } catch (e) {
    if (selected.value === objectRef) notifyError(e);
  } finally {
    commentBusy.value = false;
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
  if (saving.value || logicSaving.value || lockBusy.value) {
    notifyError("正在完成当前保存，请稍候再切换条目。");
    return;
  }
  if (selected.value === p.ref) return;
  const previousRef = selected.value;
  if (previousRef && previousRef !== p.ref && lockHeldByMe.value) {
    void setEditLock(true, previousRef, true);
  }
  const epoch = ++selectionEpoch;
  simRequestSerial += 1;
  draftRequestSerial += 1;
  selected.value = p.ref;
  resetCollabView();
  questEtag.value = "";
  hydrateError.value = "";
  hydratingQuest.value = p.kind === "quest";
  logicText.value = "";
  logicIssues.value = [];
  logicFlash.value = "";
  sim.value = null;
  simBusy.value = false;
  simError.value = "";
  questChoicesText.value = "";
  initialStateText.value = "{}";
  dialogueTreeId.value = "";
  dialogueChoicesText.value = "";
  draft.value = null;
  drafting.value = false;
  if (p.kind === "quest") {
    const id = p.ref.split(":")[1] ?? "";
    // current prerequisites are inferred from incoming dependency edges (source -> this).
    // only quest sources belong in the quest `prerequisites` field — drop event deps.
    const prereqs = (view.value?.dependencies ?? [])
      .filter((d) => d.target === p.ref && d.source.startsWith("quest:"))
      .map((d) => d.source.split(":")[1] ?? "")
      .filter(Boolean);
    edit.value = {
      title: p.label,
      objective: "",
      giverNpc: "",
      location: "",
      order: p.order,
      prereqs,
    };
    void hydrateQuest(id, p.ref, epoch);
    void loadCollab();
  } else {
    edit.value = {
      title: p.label,
      objective: "",
      giverNpc: "",
      location: "",
      order: p.order,
      prereqs: [],
    };
    hydratingQuest.value = false;
  }
}

// pull the quest's objective (not present in the timeline payload) for a faithful editor
async function hydrateQuest(id: string, objectRef: string, epoch: number): Promise<void> {
  try {
    const body = await apiGet<{
      quest: {
        title?: string;
        objective?: string;
        giver_npc?: string | null;
        location?: string | null;
        logic?: Record<string, unknown> | null;
        etag?: string;
      };
    }>(
      `/projects/${proj()}/quests/${encodeURIComponent(id)}`,
    );
    if (selected.value === objectRef && selectionEpoch === epoch) {
      if (typeof body.quest?.objective === "string") edit.value.objective = body.quest.objective;
      if (typeof body.quest?.title === "string") edit.value.title = body.quest.title;
      edit.value.giverNpc = body.quest.giver_npc ?? "";
      edit.value.location = body.quest.location ?? "";
      if (!body.quest.etag) throw new Error("服务端没有返回任务版本，已停止编辑以避免覆盖他人内容。");
      questEtag.value = body.quest.etag;
      logicText.value = body.quest.logic ? JSON.stringify(body.quest.logic, null, 2) : "";
    }
  } catch (error) {
    if (selected.value === objectRef && selectionEpoch === epoch) {
      questEtag.value = "";
      hydrateError.value = humanizeError(error);
    }
  } finally {
    if (selected.value === objectRef && selectionEpoch === epoch) {
      hydratingQuest.value = false;
    }
  }
}

async function refresh(): Promise<void> {
  const body = await apiGet<{ timeline: TimelineView }>(`/projects/${proj()}/timeline`);
  view.value = body.timeline;
  // keep selection if the ref still exists
  if (selected.value && !view.value.items.some((it) => it.ref === selected.value)) {
    selected.value = "";
    resetCollabView();
  }
}

async function patchQuestOrder(id: string, order: number | null, ifMatch: string): Promise<string> {
  const response = await apiPatch<{ etag?: string }>(`/projects/${proj()}/quests/${encodeURIComponent(id)}`, {
    timeline_order: order,
    set_timeline_order: true,
    if_match: ifMatch,
  });
  if (!response.etag) throw new Error("顺序已提交，但服务端没有返回新版本；请刷新后继续编辑。");
  return response.etag;
}

async function fetchQuestEtag(id: string): Promise<string> {
  const body = await apiGet<{ quest: { etag?: string } }>(
    `/projects/${proj()}/quests/${encodeURIComponent(id)}`,
  );
  if (!body.quest.etag) throw new Error("无法取得任务版本，已停止写入以避免覆盖他人内容。");
  return body.quest.etag;
}

async function mutateQuestOrder(objectRef: string, order: number | null): Promise<void> {
  const id = objectRef.split(":")[1] ?? "";
  if (!id) throw new Error("任务引用缺少 ID，无法调整顺序。");
  if (selected.value === objectRef && hydratingQuest.value) {
    throw new Error("任务详情仍在读取，请读取完成后再调整顺序。");
  }
  const lock = await ensureOwnLock(objectRef);
  if (!lock.ok) throw new Error("未取得任务编辑锁，顺序没有更改。");
  const keepLock = selected.value === objectRef;
  try {
    const currentEtag = keepLock && questEtag.value ? questEtag.value : await fetchQuestEtag(id);
    const nextEtag = await patchQuestOrder(id, order, currentEtag);
    if (selected.value === objectRef) questEtag.value = nextEtag;
  } finally {
    if (lock.acquired && !keepLock) await setEditLock(true, objectRef, true);
  }
}

async function patchEventOrder(id: string, order: number | null): Promise<void> {
  await apiPatch(`/projects/${proj()}/entities/${encodeURIComponent(id)}`, {
    metadata_updates: { timeline_order: order },
  });
}

async function saveEdit(): Promise<void> {
  const item = selectedItem.value;
  if (!item || saving.value || logicSaving.value) return;
  const objectRef = item.ref;
  const id = objectRef.split(":")[1] ?? "";
  const isQuest = item.kind === "quest";
  if (isQuest && (hydratingQuest.value || !questEtag.value)) {
    error.value = hydrateError.value || "任务版本仍在读取，暂不能保存。";
    return;
  }
  const draftSnapshot = {
    title: edit.value.title,
    objective: edit.value.objective,
    giverNpc: edit.value.giverNpc.trim(),
    location: edit.value.location.trim(),
    order: Number(edit.value.order),
    prereqs: [...edit.value.prereqs],
    etag: questEtag.value,
  };
  saving.value = true;
  error.value = "";
  flash.value = "";
  try {
    if (isQuest) {
      const lock = await ensureOwnLock(objectRef);
      if (!lock.ok) {
        error.value = "未取得任务编辑锁，内容没有保存。";
        return;
      }
      if (selected.value !== objectRef) {
        if (lock.acquired) await setEditLock(true, objectRef, true);
        return;
      }
      const body: Record<string, unknown> = {
        title: draftSnapshot.title,
        objective: draftSnapshot.objective,
        // The API deliberately maps an empty string to None, so saving a cleared input removes the
        // canon reference instead of silently leaving the previous giver/location in place.
        giver_npc: draftSnapshot.giverNpc,
        location: draftSnapshot.location,
        timeline_order: draftSnapshot.order,
        set_timeline_order: true,
        prerequisites: draftSnapshot.prereqs,
        if_match: draftSnapshot.etag,
      };
      const response = await apiPatch<{ etag?: string }>(
        `/projects/${proj()}/quests/${encodeURIComponent(id)}`,
        body,
      );
      if (!response.etag) throw new Error("保存完成但未返回新版本，请刷新后继续编辑。");
      if (selected.value === objectRef) questEtag.value = response.etag;
    } else {
      await apiPatch(`/projects/${proj()}/entities/${encodeURIComponent(id)}`, {
        name: draftSnapshot.title,
        metadata_updates: { timeline_order: draftSnapshot.order },
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
  if (saving.value || logicSaving.value || lockBusy.value) return;
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
    if (item.kind === "quest") await mutateQuestOrder(item.ref, best.order);
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
    if (entry.kind === "quest") await mutateQuestOrder(entry.ref, next);
    else await patchEventOrder(entry.ref.split(":")[1] ?? "", next);
    flash.value = "已排入时间线。";
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    saving.value = false;
  }
}

function focusFromRoute(): void {
  const focus = typeof route.query.focus === "string" ? route.query.focus : "";
  if (!focus || !view.value) return;
  const item = activePlaced.value.find((candidate) => candidate.ref === focus);
  if (item) onSelect(item);
}

function closeSelection(): void {
  if (saving.value || logicSaving.value || lockBusy.value) {
    notifyError("正在完成当前保存，请稍候再关闭。");
    return;
  }
  const objectRef = selected.value;
  if (objectRef && lockHeldByMe.value) void setEditLock(true, objectRef, true);
  selectionEpoch += 1;
  simRequestSerial += 1;
  draftRequestSerial += 1;
  selected.value = "";
  resetCollabView();
  sim.value = null;
  simBusy.value = false;
  simError.value = "";
  draft.value = null;
  drafting.value = false;
  hydratingQuest.value = false;
  hydrateError.value = "";
}

interface LogicIssue {
  code: string;
  message: string;
  ref?: string;
}
const logicText = ref("");
const logicIssues = ref<LogicIssue[]>([]);
const logicSaving = ref(false);
const logicFlash = ref("");

async function saveManualLogic(clear = false): Promise<void> {
  if (logicSaving.value || saving.value || !selectedIsQuest.value) return;
  const objectRef = selected.value;
  const id = selectedId.value;
  const etag = questEtag.value;
  if (hydratingQuest.value || !etag) {
    error.value = hydrateError.value || "任务版本仍在读取，暂不能保存逻辑。";
    return;
  }
  let logic: Record<string, unknown> | null = null;
  if (!clear) {
    try {
      const parsed = JSON.parse(logicText.value || "{}");
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
        throw new Error("任务逻辑必须是 JSON 对象。");
      }
      logic = parsed as Record<string, unknown>;
    } catch (error) {
      notifyError(error instanceof SyntaxError ? "JSON 格式不正确，请检查括号和引号。" : error);
      return;
    }
  }
  logicSaving.value = true;
  error.value = "";
  logicFlash.value = "";
  try {
    const lock = await ensureOwnLock(objectRef);
    if (!lock.ok) {
      error.value = "未取得任务编辑锁，逻辑没有保存。";
      return;
    }
    if (selected.value !== objectRef) {
      if (lock.acquired) await setEditLock(true, objectRef, true);
      return;
    }
    const body = await apiPatch<{
      quest: { logic?: Record<string, unknown> | null };
      logic_issues: LogicIssue[];
      etag?: string;
    }>(`/projects/${proj()}/quests/${encodeURIComponent(id)}/logic`, { logic, if_match: etag });
    if (!body.etag) throw new Error("逻辑已提交但未返回新版本，请刷新后继续编辑。");
    if (selected.value === objectRef) {
      logicText.value = body.quest.logic ? JSON.stringify(body.quest.logic, null, 2) : "";
      logicIssues.value = body.logic_issues;
      questEtag.value = body.etag;
      logicFlash.value = clear ? "已清除手工逻辑。" : "逻辑已保存并完成确定性校验。";
    }
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    logicSaving.value = false;
  }
}

async function loadInitial(): Promise<void> {
  initialLoading.value = true;
  error.value = "";
  try {
    await refresh();
    focusFromRoute();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    initialLoading.value = false;
  }
}
onMounted(loadInitial);
watch(() => route.query.focus, focusFromRoute);
onBeforeUnmount(() => {
  const objectRef = selected.value;
  const desired = objectRef ? lockDesired.get(objectRef) : undefined;
  const shouldRelease = Boolean(
    objectRef && (desired === true || (desired !== false && lockHeldByMe.value)),
  );
  selectionEpoch += 1;
  collabRequestSerial += 1;
  window.clearInterval(lockHeartbeatTimer);
  // Clear selection before the queued release runs so neither it nor an older request performs a
  // post-unmount collab refresh. Explicit intent also covers an acquire that has not reached the UI.
  selected.value = "";
  if (shouldRelease) void setEditLock(true, objectRef, true);
});
</script>

<template>
  <section>
    <PageHead overline="TIMELINE" title="时间线" purpose="任务与事件按编年排布，点星钻改序，编年违例标红。" />

    <AsyncState
      v-if="error && !view"
      state="error"
      title="时间线读取失败"
      :hint="error"
      action-label="重新加载"
      compact
      @action="loadInitial"
    />
    <p v-if="flash" class="flash">{{ flash }}</p>
    <div v-if="error && view" class="mutation-error" role="alert">
      <span>{{ error }}</span>
      <button type="button" class="ghost xs" @click="error = ''">收起</button>
    </div>

    <AsyncState v-if="initialLoading" state="loading" title="正在校准世界时间轴…" compact />
    <template v-else-if="view">
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
                role="button"
                tabindex="0"
                :aria-pressed="p.ref === selected"
                :aria-label="`${p.kind === 'event' ? '事件' : '任务'} ${p.label}，顺序 ${p.order}${p.flagged ? '，存在编年问题' : ''}`"
                @pointerdown="onPointerDown(p, $event)"
                @keydown.enter.prevent="onSelect(p)"
                @keydown.space.prevent="onSelect(p)"
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
                role="button"
                tabindex="0"
                :aria-pressed="p.ref === selected"
                :aria-label="`${p.kind === 'event' ? '事件' : '任务'} ${p.label}，顺序 ${p.order}${p.flagged ? '，存在编年问题' : ''}`"
                @pointerdown="onPointerDown(p, $event)"
                @keydown.enter.prevent="onSelect(p)"
                @keydown.space.prevent="onSelect(p)"
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

            <div v-if="selectedIsQuest" class="edit-lock" :class="{ mine: lockHeldByMe, blocked: lockedByOther }">
              <span v-if="lockedByOther">@{{ collab.lock?.holder }} 正在编辑；当前为只读。</span>
              <span v-else-if="lockHeldByMe">已由你锁定，保存时同时校验版本。</span>
              <span v-else>开始修改前请锁定，避免多人覆盖。</span>
              <button
                class="ghost xs"
                :disabled="lockBusy || saving || logicSaving"
                @click="setEditLock(lockHeldByMe)"
              >{{ lockBusy ? "处理中…" : lockHeldByMe ? "释放锁" : "锁定编辑" }}</button>
            </div>

            <p v-if="hydratingQuest" class="hydrate-note" role="status">正在读取任务详情与版本…</p>
            <p v-else-if="hydrateError" class="hydrate-error" role="alert">{{ hydrateError }}</p>

            <label class="f">
              <span>{{ selectedIsQuest ? "标题" : "名称" }}</span>
              <input
                v-model="edit.title"
                maxlength="160"
                :disabled="saving || logicSaving || (selectedIsQuest && (lockedByOther || hydratingQuest || !questEtag))"
              />
            </label>

            <label v-if="selectedIsQuest" class="f">
              <span>目标</span>
              <textarea
                v-model="edit.objective"
                rows="3"
                :disabled="saving || logicSaving || lockedByOther || hydratingQuest || !questEtag"
              ></textarea>
            </label>

            <template v-if="selectedIsQuest">
              <label class="f">
                <span>任务发布人（实体 id）</span>
                <input
                  v-model="edit.giverNpc"
                  :disabled="saving || logicSaving || lockedByOther || hydratingQuest || !questEtag"
                  placeholder="例如 npc_elder；留空保存可清除"
                />
              </label>

              <label class="f">
                <span>任务地点（实体 id）</span>
                <input
                  v-model="edit.location"
                  :disabled="saving || logicSaving || lockedByOther || hydratingQuest || !questEtag"
                  placeholder="例如 loc_canyon；留空保存可清除"
                />
              </label>
            </template>

            <label class="f narrow">
              <span>时间顺序</span>
              <input
                v-model.number="edit.order"
                type="number"
                step="1"
                :disabled="saving || logicSaving || (selectedIsQuest && (lockedByOther || hydratingQuest || !questEtag))"
              />
            </label>

            <template v-if="selectedIsQuest">
              <div class="section sub"><span class="t">前置任务</span></div>
              <p v-if="!otherQuests.length" class="muted small">还没有其它任务可作前置。</p>
              <div v-else class="prereqs">
                <label v-for="q in otherQuests" :key="q.id" class="pq">
                  <input
                    type="checkbox"
                    :checked="edit.prereqs.includes(q.id)"
                    :disabled="saving || logicSaving || lockedByOther || hydratingQuest || !questEtag"
                    @change="togglePrereq(q.id)"
                  />
                  <span>{{ q.label }}</span>
                </label>
              </div>
            </template>

            <div class="d-actions">
              <button
                class="primary"
                :disabled="saving || logicSaving || lockedByOther || (selectedIsQuest && (hydratingQuest || !questEtag))"
                @click="saveEdit"
              >保存</button>
              <button class="ghost" :disabled="saving || logicSaving || lockBusy" @click="closeSelection">关闭</button>
            </div>

            <details v-if="selectedIsQuest" class="playtest" open>
              <summary>任务试玩 <span class="muted">确定性 · $0 · 不写入正典</span></summary>
              <p class="muted small">
                任务分支填 branch id；对白选择填 node_id:index。多个选择可用逗号、空格或换行分隔。
              </p>
              <label class="f">
                <span>任务分支选择（可选）</span>
                <input v-model="questChoicesText" placeholder="branch_help, branch_leave" />
              </label>
              <label class="f">
                <span>初始变量 JSON</span>
                <textarea
                  v-model="initialStateText"
                  rows="4"
                  class="mono"
                  placeholder='{"has_key": true, "reputation": 2}'
                ></textarea>
              </label>
              <div class="playtest-dialogue">
                <label class="f">
                  <span>先走对白树（可选）</span>
                  <input v-model="dialogueTreeId" placeholder="dialogue_tree_id" />
                </label>
                <label class="f">
                  <span>对白选择（可选）</span>
                  <input
                    v-model="dialogueChoicesText"
                    :disabled="!dialogueTreeId.trim()"
                    placeholder="node_intro:0, node_gate:1"
                  />
                </label>
              </div>
              <p class="muted small">
                对白产生的 final_state 会注入任务；上面的初始变量在同名键上优先。
              </p>
              <button class="ghost" :disabled="simBusy" @click="runSim">
                {{ simBusy ? "试玩中…" : "开始试玩" }}
              </button>
              <p v-if="simError" class="playtest-error" role="alert">{{ simError }}</p>

              <div v-if="sim?.dialogue_walk" class="sim-result">
                <div class="sim-heading">
                  <b>对白结果</b>
                  <span class="sim-status" :class="sim.dialogue_walk.status">
                    {{ SIM_LABEL[sim.dialogue_walk.status] ?? sim.dialogue_walk.status }}
                  </span>
                </div>
                <span class="sim-path mono">
                  {{ sim.dialogue_walk.path.join(" → ") || "（无对白节点）" }}
                </span>
                <span v-if="sim.dialogue_walk.message" class="muted sim-msg">
                  {{ sim.dialogue_walk.message }}
                </span>
                <ul v-if="sim.dialogue_walk.steps.length" class="sim-steps">
                  <li v-for="step in sim.dialogue_walk.steps" :key="step.node_id">
                    <code>{{ step.node_id }}</code>
                    <span v-if="step.chosen">选择 {{ step.chosen }}</span>
                    <span v-if="step.effects.length">效果 {{ step.effects.join("；") }}</span>
                  </li>
                </ul>
                <span class="muted small">对白结束状态</span>
                <pre class="sim-state mono">{{ formatState(sim.dialogue_walk.final_state) }}</pre>
              </div>

              <div v-if="sim" class="sim-result">
                <div class="sim-heading">
                  <b>任务结果</b>
                  <span class="sim-status" :class="sim.run.status">
                    {{ SIM_LABEL[sim.run.status] ?? sim.run.status }}
                  </span>
                </div>
                <span class="sim-path mono">{{ sim.run.path.join(" → ") || "（未进入任务阶段）" }}</span>
                <span v-if="sim.run.message" class="muted sim-msg">{{ sim.run.message }}</span>
                <ul v-if="sim.run.steps.length" class="sim-steps">
                  <li v-for="step in sim.run.steps" :key="step.stage_id">
                    <code>{{ step.stage_id }}</code>
                    <span v-if="step.branch_taken">分支 {{ step.branch_taken }}</span>
                    <span v-if="step.effects.length">效果 {{ step.effects.join("；") }}</span>
                  </li>
                </ul>
                <span class="muted small">任务结束状态</span>
                <pre class="sim-state mono">{{ formatState(sim.run.final_state) }}</pre>
              </div>
            </details>

            <details v-if="selectedIsQuest" class="manual-logic">
              <summary>手工任务逻辑 <span class="muted">高级 · 保存即校验</span></summary>
              <p class="muted small">
                JSON 字段：variables、precondition、stage_logic、branches、unlocks。留空可用“清除逻辑”。
              </p>
              <textarea
                v-model="logicText"
                rows="12"
                class="logic-json mono"
                :disabled="lockedByOther || logicSaving || saving || hydratingQuest || !questEtag"
                placeholder='{"variables": [], "stage_logic": [], "branches": [], "unlocks": []}'
              ></textarea>
              <p v-if="logicFlash" class="flash small" role="status">{{ logicFlash }}</p>
              <ul v-if="logicIssues.length" class="b7-issues">
                <li v-for="(issue, index) in logicIssues" :key="`${issue.code}-${index}`">
                  {{ issue.code }}：{{ issue.message }}
                </li>
              </ul>
              <p v-else-if="logicFlash" class="b7-clean">确定性校验未发现问题。</p>
              <div class="logic-actions">
                <button class="ghost" :disabled="logicSaving || saving || lockedByOther || hydratingQuest || !questEtag" @click="saveManualLogic(false)">
                  {{ logicSaving ? "校验中…" : "保存并校验" }}
                </button>
                <button class="ghost danger-soft" :disabled="logicSaving || saving || lockedByOther || hydratingQuest || !questEtag || !logicText" @click="saveManualLogic(true)">
                  清除逻辑
                </button>
              </div>
            </details>

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

            <div
              v-if="selectedIsQuest"
              class="collab"
              :data-collab-snapshot="collabSnapshotEtag || undefined"
              :aria-busy="collabLoading || collabLoadingMore || undefined"
            >
              <div class="collab-assign">
                <span class="muted">负责人：</span>
                <span v-if="collab.assignee" class="assignee">@{{ collab.assignee }}</span>
                <span v-else class="muted">未指派</span>
                <button class="ghost xs" :disabled="collabLoading" @click="assignToMe(false)">指派给我</button>
                <button v-if="collab.assignee" class="ghost xs" :disabled="collabLoading" @click="assignToMe(true)">取消</button>
              </div>
              <div v-for="c in collab.comments" :key="c.id" class="cmt">
                <span class="cmt-author">{{ c.author }}</span>
                <span class="cmt-body">{{ c.body }}</span>
              </div>
              <div v-if="collabTotal" class="collab-page-status">
                <span class="muted">已显示 {{ collab.comments.length }} / {{ collabTotal }} 条（最新在前）</span>
                <button
                  v-if="collabNextCursor"
                  class="ghost xs"
                  :disabled="collabLoading || collabLoadingMore"
                  @click="loadCollab(selected, true)"
                >{{ collabLoadingMore ? "读取中…" : "加载更早评论" }}</button>
              </div>
              <div class="collab-add">
                <input v-model="draftComment" placeholder="留个评论…" @keydown.enter="postComment" />
                <button
                  class="ghost xs"
                  :disabled="commentBusy || collabLoading || !draftComment.trim()"
                  @click="postComment"
                >{{ commentBusy ? "提交中…" : "评论" }}</button>
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
.node:focus-visible {
  outline: none;
}
.node:focus-visible .station-ring {
  stroke: var(--ow-cyan);
  stroke-width: 3;
  filter: drop-shadow(0 0 5px rgba(143, 214, 232, 0.72));
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
.edit-lock {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 0.45rem 0.55rem;
  border: 1px solid var(--ow-line);
  background: rgba(143, 214, 232, 0.05);
  color: var(--ow-muted);
  font-size: 0.76rem;
}
.edit-lock.mine { border-color: rgba(142, 212, 172, 0.4); color: #8ed4ac; }
.edit-lock.blocked { border-color: rgba(224, 133, 133, 0.45); color: #e89a9a; }
.manual-logic {
  margin-top: 0.7rem;
  padding-top: 0.6rem;
  border-top: 1px solid var(--ow-line);
}
.manual-logic summary { color: var(--ow-gold-bright); cursor: pointer; font-size: 0.82rem; }
.manual-logic summary span { margin-left: 0.35rem; font-size: 0.72rem; }
.logic-json { width: 100%; margin-top: 0.45rem; resize: vertical; tab-size: 2; }
.logic-actions { display: flex; gap: 0.45rem; margin-top: 0.45rem; }
.danger-soft { color: #e89a9a; border-color: rgba(224, 133, 133, 0.35); }
input:disabled,
textarea:disabled { opacity: 0.62; cursor: not-allowed; }
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
.playtest {
  margin-top: 0.7rem;
  padding-top: 0.6rem;
  border-top: 1px solid var(--ow-line);
}
.playtest summary {
  color: var(--ow-gold-bright);
  cursor: pointer;
  font-size: 0.82rem;
}
.playtest summary span {
  margin-left: 0.35rem;
  font-size: 0.72rem;
}
.playtest > .f,
.playtest-dialogue {
  margin-top: 0.5rem;
}
.playtest-dialogue {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 0.5rem;
}
.playtest-error {
  color: #e89a9a;
  font-size: 0.78rem;
  margin: 0.45rem 0 0;
}
.sim-result {
  display: flex;
  flex-direction: column;
  align-items: stretch;
  gap: 0.5rem;
  margin-top: 0.6rem;
  padding: 0.6rem;
  border: 1px solid var(--ow-line);
  background: rgba(143, 214, 232, 0.04);
}
.sim-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.8rem;
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
.sim-status.cycle,
.sim-status.unavailable,
.sim-status.dangling {
  color: var(--ow-flag, #e0653a);
  border-color: var(--ow-flag, #e0653a);
}
.sim-path {
  font-size: 0.78rem;
  color: var(--ow-muted);
}
.sim-msg {
  font-size: 0.74rem;
}
.sim-steps {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  margin: 0;
  padding-left: 1.1rem;
  color: var(--ow-muted);
  font-size: 0.74rem;
}
.sim-steps li span {
  margin-left: 0.45rem;
}
.sim-state {
  max-height: 10rem;
  overflow: auto;
  margin: -0.2rem 0 0;
  padding: 0.45rem;
  border: 1px solid var(--ow-line);
  background: var(--ow-panel-2);
  color: var(--ow-ink);
  font-size: 0.72rem;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
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
  color: var(--ow-muted);
  white-space: nowrap;
}
.collab-page-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.4rem;
  font-size: 0.7rem;
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
  min-height: 44px;
  padding: 0.35rem 0.65rem;
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
.mutation-error,
.hydrate-error,
.hydrate-note {
  margin: 0.55rem 0;
  border: 1px solid rgba(224, 112, 90, 0.42);
  background: rgba(224, 112, 90, 0.09);
  color: #efaa9a;
  padding: 0.55rem 0.7rem;
  line-height: 1.55;
}
.mutation-error {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
}
.hydrate-note {
  border-color: rgba(143, 214, 232, 0.32);
  background: rgba(143, 214, 232, 0.07);
  color: var(--ow-cyan);
}

@media (max-width: 820px) {
  .layout {
    grid-template-columns: 1fr;
  }
  .playtest-dialogue {
    grid-template-columns: 1fr;
  }
  .edit-lock,
  .logic-actions { align-items: stretch; flex-direction: column; }
  .edit-lock button,
  .logic-actions button { width: 100%; min-height: 44px; }
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
