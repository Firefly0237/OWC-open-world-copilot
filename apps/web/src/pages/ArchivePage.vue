<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter, type LocationQueryRaw } from "vue-router";
import {
  ApiError,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  currentOperator,
  currentProject,
  humanizeError,
} from "../api";
import AsyncState from "../components/AsyncState.vue";
import Modal from "../components/Modal.vue";
import PageHead from "../components/PageHead.vue";
import { askConfirm } from "../confirm";
import {
  ARCHIVE_KINDS,
  isArchiveKind,
  kindFromRef,
  locateDialogueParent,
  locationForObject,
  objectIdFromRef,
  type ArchiveKind,
  type DialogueParentLocation,
  type DialogueTreePayload,
} from "../objectNavigation";
import { notifyError, notifyOk } from "../toast";

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
interface QuestRow {
  id: string;
  title: string;
  giver_npc: string;
  location: string;
  objective: string;
  stages: number;
  timeline_order: number | null;
  origin: string;
  review_status: string;
}
interface RegionRow {
  id: string;
  name: string;
  level_min: number;
  level_max: number;
  themes: string;
  banned_content: string;
}
interface PoiRow {
  id: string;
  name: string;
  region_id: string;
  purpose: string;
  controlling_faction: string;
}
interface TermRow {
  id: string;
  canonical: string;
  aliases: string;
  forbidden: string;
  description: string;
}
interface DialogueRow {
  id: string;
  text_key: string;
  speaker_id: string;
  quest_id: string;
  text: string;
}
interface DialogueTreeRow {
  id: string;
  title: string;
  quest_id: string;
  participants: string;
  nodes: number;
}
interface RelationRow {
  source: string;
  kind: string;
  target: string;
  valid_from: number | null;
  valid_until: number | null;
  origin: string;
  review_status: string;
  metadata?: Record<string, unknown>;
}
interface StyleGuideRow {
  id: string;
  body: string;
  rules: string[];
  origin: string;
  review_status: string;
}
interface QuestEventRefRow {
  id: string;
  quest_id: string;
  event_id: string;
  ref_kind: string;
  note: string;
  origin: string;
  review_status: string;
  metadata?: Record<string, unknown>;
}
interface StyleRubricRow {
  id: string;
  label: string;
  abstract_tag: string;
  version: string;
  frozen_at: string | null;
  author: string;
  elements: Array<Record<string, unknown>>;
  element_count: number;
  origin: string;
  review_status: string;
  metadata?: Record<string, unknown>;
}
interface ArchiveInventory {
  entities: EntityRow[];
  quests: QuestRow[];
  regions: RegionRow[];
  pois: PoiRow[];
  terms: TermRow[];
  dialogues: DialogueRow[];
  dialogue_trees: DialogueTreeRow[];
  relations: RelationRow[];
  style_guides: StyleGuideRow[];
  quest_event_refs: QuestEventRefRow[];
  style_rubrics: StyleRubricRow[];
  dialogue_tree_payloads: Record<string, DialogueTreePayload>;
}
interface ArchivePageResponse {
  project: string;
  kind: ArchiveKind;
  items: Array<Record<string, unknown>>;
  counts: Partial<Record<ArchiveKind, number>>;
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
  offset?: number;
  page_cursor?: string | null;
  previous_cursor?: string | null;
  dialogue_tree_payloads?: Record<string, DialogueTreePayload>;
}
interface ArchiveItem {
  kind: ArchiveKind;
  id: string;
  ref: string;
  title: string;
  summary: string;
  meta: string;
  raw: Record<string, unknown>;
  dialogueParent?: DialogueParentLocation | null;
}
interface DetailRow {
  label: string;
  value: string;
}
interface AssetRow {
  id: string;
  kind: string;
  uri: string;
  title: string;
}
interface CollabAssignment {
  assignee: string;
  assigned_by: string;
  at: string;
  note: string;
}
interface CollabComment {
  id: string;
  author: string;
  body: string;
  at: string;
}
interface EditLock {
  holder: string;
  at: string;
}
interface CollabSnapshot {
  assignment: CollabAssignment | null;
  comments: CollabComment[];
  lock: EditLock | null;
}
interface CollabPageResponse extends CollabSnapshot {
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
}

const PAGE_SIZE = 20;
const COLLAB_PAGE_SIZE = 20;
const ASSET_PAGE_SIZE = 20;
const route = useRoute();
const router = useRouter();

const KIND_CONFIGS: Array<{ kind: ArchiveKind; label: string; empty: string }> = [
  { kind: "entity", label: "实体", empty: "还没有实体。" },
  { kind: "quest", label: "任务", empty: "还没有任务。" },
  { kind: "region", label: "区域", empty: "还没有区域。" },
  { kind: "poi", label: "地点", empty: "还没有兴趣点。" },
  { kind: "term", label: "词条", empty: "还没有术语词条。" },
  { kind: "dialogue", label: "对话", empty: "还没有独立对话条目。" },
  { kind: "dialogue_tree", label: "对话树", empty: "还没有对话树。" },
  { kind: "relation", label: "关系", empty: "还没有正典关系。" },
  { kind: "style_guide", label: "风格指南", empty: "还没有风格指南。" },
  { kind: "quest_event_ref", label: "任务事件引用", empty: "还没有任务事件引用。" },
  { kind: "style_rubric", label: "风格量表", empty: "还没有风格量表。" },
];
const KIND_LABELS = Object.fromEntries(
  KIND_CONFIGS.map((config) => [config.kind, config.label]),
) as Record<ArchiveKind, string>;
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
const ASSET_KIND_LABELS: Record<string, string> = {
  image: "图像",
  audio: "音频",
  map: "地图",
  link: "链接",
};

function emptyInventory(): ArchiveInventory {
  return {
    entities: [],
    quests: [],
    regions: [],
    pois: [],
    terms: [],
    dialogues: [],
    dialogue_trees: [],
    relations: [],
    style_guides: [],
    quest_event_refs: [],
    style_rubrics: [],
    dialogue_tree_payloads: {},
  };
}

function routeText(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function routeKind(): ArchiveKind {
  const focusKind = kindFromRef(routeText(route.query.focus));
  if (focusKind) return focusKind;
  return isArchiveKind(route.query.kind) ? route.query.kind : "entity";
}

function routePage(): number {
  const value = Number(routeText(route.query.page));
  return Number.isInteger(value) && value > 0 ? value : 1;
}

function routeCursor(): string {
  return routeText(route.query.cursor);
}

const inventory = ref<ArchiveInventory>(emptyInventory());
const loading = ref(true);
const loadError = ref("");
const currentKind = ref<ArchiveKind>(routeKind());
const query = ref(routeText(route.query.q));
const page = ref(routePage());
const focusRef = ref(routeText(route.query.focus));
const counts = ref<Record<ArchiveKind, number>>(
  Object.fromEntries(ARCHIVE_KINDS.map((kind) => [kind, 0])) as Record<ArchiveKind, number>,
);
const currentTotal = ref(0);
const currentOffset = ref(0);
const currentCursor = ref(routeCursor());
const nextCursor = ref<string | null>(null);
const previousCursor = ref<string | null>(null);
const cursorTrail = ref<string[]>([""]);
const snapshotEtag = ref("");
const pageLoading = ref(false);
const detail = ref<ArchiveItem | null>(null);
const showRaw = ref(false);
let applyingRoute = false;
let skipNextRouteLoad = false;
let loadSequence = 0;
let searchTimer = 0;

function rawRecord<T extends object>(row: T): Record<string, unknown> {
  return row as unknown as Record<string, unknown>;
}

const itemsByKind = computed<Record<ArchiveKind, ArchiveItem[]>>(() => ({
  entity: inventory.value.entities.map((row) => ({
    kind: "entity",
    id: row.id,
    ref: `entity:${row.id}`,
    title: row.name || row.id,
    summary: row.description,
    meta: [TYPE_LABELS[row.type] ?? row.type, row.tags].filter(Boolean).join(" · "),
    raw: rawRecord(row),
  })),
  quest: inventory.value.quests.map((row) => ({
    kind: "quest",
    id: row.id,
    ref: `quest:${row.id}`,
    title: row.title || row.id,
    summary: row.objective,
    meta: [row.giver_npc && `委托人 ${row.giver_npc}`, row.location && `地点 ${row.location}`]
      .filter(Boolean).join(" · "),
    raw: rawRecord(row),
  })),
  region: inventory.value.regions.map((row) => ({
    kind: "region",
    id: row.id,
    ref: `region:${row.id}`,
    title: row.name || row.id,
    summary: row.themes,
    meta: `等级 ${row.level_min}–${row.level_max}`,
    raw: rawRecord(row),
  })),
  poi: inventory.value.pois.map((row) => ({
    kind: "poi",
    id: row.id,
    ref: `poi:${row.id}`,
    title: row.name || row.id,
    summary: row.purpose,
    meta: row.region_id ? `区域 ${row.region_id}` : "未绑定区域",
    raw: rawRecord(row),
  })),
  term: inventory.value.terms.map((row) => ({
    kind: "term",
    id: row.id,
    ref: `term:${row.id}`,
    title: row.canonical || row.id,
    summary: row.description,
    meta: row.aliases ? `别名 ${row.aliases}` : "无别名",
    raw: rawRecord(row),
  })),
  dialogue: inventory.value.dialogues.map((row) => ({
    kind: "dialogue",
    id: row.id,
    ref: `dialogue:${row.id}`,
    title: row.text_key || row.id,
    summary: row.text,
    meta: [row.speaker_id && `说话人 ${row.speaker_id}`, row.quest_id && `任务 ${row.quest_id}`]
      .filter(Boolean).join(" · "),
    raw: rawRecord(row),
    dialogueParent: locateDialogueParent(row, inventory.value.dialogue_tree_payloads),
  })),
  dialogue_tree: inventory.value.dialogue_trees.map((row) => ({
    kind: "dialogue_tree",
    id: row.id,
    ref: `dialogue_tree:${row.id}`,
    title: row.title || row.id,
    summary: row.participants ? `参与者：${row.participants}` : "未指定参与者",
    meta: `${row.nodes} 个节点${row.quest_id ? ` · 任务 ${row.quest_id}` : ""}`,
    raw: rawRecord(row),
  })),
  relation: inventory.value.relations.map((row) => ({
    kind: "relation",
    id: `${row.source}:${row.kind}:${row.target}`,
    ref: `relation:${row.source}:${row.kind}:${row.target}`,
    title: `${row.source} → ${row.target}`,
    summary: row.kind,
    meta: [
      row.valid_from != null && `起始 ${row.valid_from}`,
      row.valid_until != null && `截止 ${row.valid_until}`,
      row.review_status,
    ].filter(Boolean).join(" · "),
    raw: rawRecord(row),
  })),
  style_guide: inventory.value.style_guides.map((row) => ({
    kind: "style_guide",
    id: row.id,
    ref: `style_guide:${row.id}`,
    title: row.id,
    summary: row.body,
    meta: `${row.rules.length} 条规则 · ${row.review_status}`,
    raw: rawRecord(row),
  })),
  quest_event_ref: inventory.value.quest_event_refs.map((row) => ({
    kind: "quest_event_ref",
    id: row.id,
    ref: `quest_event_ref:${row.id}`,
    title: `${row.quest_id} → ${row.event_id}`,
    summary: row.note || row.ref_kind,
    meta: `${row.ref_kind} · ${row.review_status}`,
    raw: rawRecord(row),
  })),
  style_rubric: inventory.value.style_rubrics.map((row) => ({
    kind: "style_rubric",
    id: row.id,
    ref: `style_rubric:${row.id}`,
    title: row.label || row.id,
    summary: row.elements
      .map((element) => String(element.text ?? ""))
      .filter(Boolean)
      .slice(0, 2)
      .join("；") || row.abstract_tag,
    meta: `${row.version} · ${row.element_count} 项 · ${row.frozen_at ? "已冻结" : "草稿"}`,
    raw: rawRecord(row),
  })),
}));

const totalItems = computed(() => ARCHIVE_KINDS.reduce(
  (sum, kind) => sum + counts.value[kind],
  0,
));
const currentItems = computed(() => itemsByKind.value[currentKind.value]);
// `inventory` contains only the current server page.  Keeping this alias makes the template and
// detail mapping explicit without ever slicing an already-downloaded full archive in the browser.
const pagedItems = computed(() => currentItems.value);
const pageCount = computed(() => Math.max(1, Math.ceil(currentTotal.value / PAGE_SIZE)));
const rangeLabel = computed(() => {
  if (!currentTotal.value) return "0 条";
  const start = currentOffset.value + 1;
  const end = Math.min(currentOffset.value + currentItems.value.length, currentTotal.value);
  return `${start}–${end} / ${currentTotal.value}`;
});
const currentConfig = computed(() =>
  KIND_CONFIGS.find((config) => config.kind === currentKind.value) ?? KIND_CONFIGS[0],
);

function managedQueryMatches(): boolean {
  return routeText(route.query.kind) === currentKind.value
    && routeText(route.query.q) === query.value.trim()
    && routeText(route.query.page) === String(page.value)
    && routeText(route.query.focus) === focusRef.value
    && routeText(route.query.cursor) === currentCursor.value;
}

async function syncRouteState(skipLoad = false): Promise<void> {
  if (applyingRoute || managedQueryMatches()) return;
  const next: LocationQueryRaw = { ...route.query };
  next.kind = currentKind.value;
  next.page = String(page.value);
  if (query.value.trim()) next.q = query.value.trim();
  else delete next.q;
  if (focusRef.value) next.focus = focusRef.value;
  else delete next.focus;
  if (currentCursor.value) next.cursor = currentCursor.value;
  else delete next.cursor;
  if (skipLoad) skipNextRouteLoad = true;
  try {
    await router.replace({ path: route.path, query: next });
  } catch (error) {
    if (skipLoad) skipNextRouteLoad = false;
    throw error;
  }
}

function resetPaging(): void {
  // Invalidate an older page response immediately; a debounced search must not briefly paint the
  // previous query while the replacement request is waiting to start.
  loadSequence += 1;
  page.value = 1;
  currentOffset.value = 0;
  currentCursor.value = "";
  nextCursor.value = null;
  previousCursor.value = null;
  cursorTrail.value = [""];
  snapshotEtag.value = "";
}

watch(query, (value, previous) => {
  if (applyingRoute || value === previous) return;
  window.clearTimeout(searchTimer);
  resetPaging();
  focusRef.value = "";
  resetDetailState();
  searchTimer = window.setTimeout(() => void syncRouteState(), 250);
});

function selectKind(kind: ArchiveKind): void {
  if (currentKind.value === kind) return;
  currentKind.value = kind;
  resetPaging();
  focusRef.value = "";
  resetDetailState();
  void syncRouteState();
}

function clearSearch(): void {
  query.value = "";
}

const canGoPrevious = computed(() =>
  page.value > 1
  && (previousCursor.value !== null || cursorTrail.value[page.value - 2] !== undefined),
);

function goPrevious(): void {
  if (!canGoPrevious.value || pageLoading.value) return;
  const known = cursorTrail.value[page.value - 2];
  const cursor = previousCursor.value ?? known;
  if (cursor === undefined) return;
  currentCursor.value = cursor;
  page.value -= 1;
  currentOffset.value = Math.max(0, currentOffset.value - PAGE_SIZE);
  focusRef.value = "";
  resetDetailState();
  void syncRouteState();
}

function goNext(): void {
  if (!nextCursor.value || pageLoading.value) return;
  cursorTrail.value[page.value] = nextCursor.value;
  currentCursor.value = nextCursor.value;
  page.value += 1;
  currentOffset.value += PAGE_SIZE;
  focusRef.value = "";
  resetDetailState();
  void syncRouteState();
}

// ---- detail / entity edit ---------------------------------------------------------------
const edit = ref({ name: "", description: "", tags: "" });
const saving = ref(false);
const assets = ref<AssetRow[]>([]);
const assetsLoading = ref(false);
const assetsLoadingMore = ref(false);
const assetTotal = ref(0);
const assetNextCursor = ref<string | null>(null);
const newAsset = ref({ kind: "image", uri: "", title: "" });
const collab = ref<CollabSnapshot>({ assignment: null, comments: [], lock: null });
const collabLoading = ref(false);
const collabLoadingMore = ref(false);
const collabTotal = ref(0);
const collabNextCursor = ref<string | null>(null);
const collabSnapshotEtag = ref("");
const assignmentBusy = ref(false);
const commentBusy = ref(false);
const draftComment = ref("");
const lockBusy = ref(false);
let lockBusyCount = 0;
let lockHeartbeatTimer = 0;
let collabRequestSerial = 0;
let assetsRequestSerial = 0;
const lockDesired = new Map<string, boolean>();
const lockRequestChains = new Map<string, Promise<void>>();
const activeObjectRef = computed(() => detail.value?.ref ?? "");
const detailEntity = computed(() =>
  detail.value?.kind === "entity" ? detail.value.raw as unknown as EntityRow : null,
);
const canDeleteDetail = computed(() =>
  detail.value?.kind === "entity" || detail.value?.kind === "dialogue_tree",
);

function meName(): string {
  return currentOperator().trim();
}

const lockHeldByMe = computed(() => Boolean(
  meName() && collab.value.lock?.holder === meName(),
));
const lockedByOther = computed(() => Boolean(collab.value.lock && !lockHeldByMe.value));
const collaborationStatus = computed(() => {
  if (collabLoading.value) return "同步中";
  if (lockedByOther.value) return `由 @${collab.value.lock?.holder ?? "他人"} 编辑中`;
  if (lockHeldByMe.value) return "你正在编辑";
  if (collab.value.assignment) return "已指派 · 可编辑";
  return "待认领 · 可编辑";
});

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

function clearCollabView(): void {
  collabRequestSerial += 1;
  collab.value = { assignment: null, comments: [], lock: null };
  collabLoading.value = false;
  collabLoadingMore.value = false;
  collabTotal.value = 0;
  collabNextCursor.value = null;
  collabSnapshotEtag.value = "";
  draftComment.value = "";
}

async function loadCollab(
  objectRef = activeObjectRef.value,
  append = false,
): Promise<void> {
  if (!objectRef) {
    clearCollabView();
    return;
  }
  const cursor = append ? collabNextCursor.value : null;
  if (append && (!cursor || collabLoadingMore.value)) return;
  const requestSerial = ++collabRequestSerial;
  if (activeObjectRef.value === objectRef) {
    if (append) collabLoadingMore.value = true;
    else {
      collabLoading.value = true;
      collabLoadingMore.value = false;
    }
  }
  try {
    const cursorQuery = cursor ? `&cursor=${encodeURIComponent(cursor)}` : "";
    const body = await apiGet<CollabPageResponse>(
      `/projects/${currentProject()}/collab?object_ref=${encodeURIComponent(objectRef)}`
        + `&limit=${COLLAB_PAGE_SIZE}${cursorQuery}`,
    );
    if (activeObjectRef.value === objectRef && collabRequestSerial === requestSerial) {
      collab.value = {
        assignment: body.assignment ?? null,
        comments: append
          ? [...collab.value.comments, ...(body.comments ?? [])]
          : body.comments ?? [],
        lock: body.lock ?? null,
      };
      collabTotal.value = body.total;
      collabNextCursor.value = body.next_cursor;
      collabSnapshotEtag.value = body.snapshot_etag;
    }
  } catch (error) {
    if (activeObjectRef.value === objectRef && collabRequestSerial === requestSerial) {
      if (append && error instanceof ApiError && error.status === 409) {
        // A new comment invalidates the continuation cursor.  Replace the visible thread with a
        // fresh newest-first page instead of mixing two snapshots or dropping the new comment.
        collabLoadingMore.value = false;
        await loadCollab(objectRef);
        return;
      }
      notifyError(error);
    }
  } finally {
    if (activeObjectRef.value === objectRef && collabRequestSerial === requestSerial) {
      if (append) collabLoadingMore.value = false;
      else collabLoading.value = false;
    }
  }
}

async function setEditLock(
  release: boolean,
  objectRef = activeObjectRef.value,
  quiet = false,
): Promise<boolean> {
  const holder = meName();
  if (!holder) {
    if (!quiet) notifyError("请先在审阅台填写署名，再锁定编辑。");
    return false;
  }
  if (!objectRef) return false;
  // Release intent is recorded before queueing so an already queued heartbeat cannot renew later.
  lockDesired.set(objectRef, !release);
  markLockBusy(1);
  try {
    return await queueLockRequest(objectRef, async () => {
      if (!release && lockDesired.get(objectRef) !== true) return false;
      await apiPost(`/projects/${currentProject()}/collab/lock`, {
        object_ref: objectRef,
        holder,
        release,
      });
      if (release && lockDesired.get(objectRef) === false) lockDesired.delete(objectRef);
      if (activeObjectRef.value === objectRef) await loadCollab(objectRef);
      return true;
    });
  } catch (error) {
    if (!quiet) notifyError(error);
    if (activeObjectRef.value === objectRef) await loadCollab(objectRef);
    return false;
  } finally {
    markLockBusy(-1);
  }
}

function releaseDetailLock(objectRef: string): void {
  if (!objectRef) return;
  const desired = lockDesired.get(objectRef);
  const visiblyHeld = activeObjectRef.value === objectRef && lockHeldByMe.value;
  if (desired === true || (desired !== false && visiblyHeld)) {
    void setEditLock(true, objectRef, true);
  }
}

interface LockResult {
  ok: boolean;
  acquired: boolean;
}

async function ensureOwnLock(objectRef: string): Promise<LockResult> {
  let believedMine = activeObjectRef.value === objectRef && lockHeldByMe.value;
  if (activeObjectRef.value === objectRef && lockedByOther.value) {
    await loadCollab(objectRef);
    if (lockedByOther.value) {
      notifyError(`“${collab.value.lock?.holder}”正在编辑这个对象，请稍后刷新。`);
      return { ok: false, acquired: false };
    }
    believedMine = lockHeldByMe.value;
  }
  const ok = await setEditLock(false, objectRef);
  return { ok, acquired: ok && !believedMine };
}

function syncLockHeartbeat(): void {
  window.clearInterval(lockHeartbeatTimer);
  lockHeartbeatTimer = 0;
  const objectRef = activeObjectRef.value;
  if (!objectRef || !lockHeldByMe.value || lockDesired.get(objectRef) === false) return;
  const holder = meName();
  lockDesired.set(objectRef, true);
  lockHeartbeatTimer = window.setInterval(() => {
    if (
      activeObjectRef.value !== objectRef
      || !lockHeldByMe.value
      || meName() !== holder
      || lockDesired.get(objectRef) !== true
    ) return;
    void queueLockRequest(objectRef, async () => {
      if (
        activeObjectRef.value !== objectRef
        || !lockHeldByMe.value
        || meName() !== holder
        || lockDesired.get(objectRef) !== true
      ) return;
      try {
        await apiPost(`/projects/${currentProject()}/collab/lock`, {
          object_ref: objectRef,
          holder,
          release: false,
        });
      } catch (error) {
        if (activeObjectRef.value === objectRef && lockDesired.get(objectRef) === true) {
          notifyError(`编辑锁续期失败，请刷新锁状态。${humanizeError(error)}`);
          await loadCollab(objectRef);
        }
      }
    });
  }, 45_000);
}

watch(() => `${activeObjectRef.value}|${lockHeldByMe.value}`, syncLockHeartbeat);

async function assignToMe(clear = false): Promise<void> {
  const objectRef = activeObjectRef.value;
  const operator = meName();
  if (!operator) {
    notifyError("请先在审阅台填写署名，再认领对象。");
    return;
  }
  if (!objectRef || assignmentBusy.value) return;
  assignmentBusy.value = true;
  try {
    await apiPost(`/projects/${currentProject()}/collab/assign`, {
      object_ref: objectRef,
      assignee: clear ? "" : operator,
      by: operator,
    });
    if (activeObjectRef.value === objectRef) await loadCollab(objectRef);
  } catch (error) {
    if (activeObjectRef.value === objectRef) notifyError(error);
  } finally {
    assignmentBusy.value = false;
  }
}

async function postComment(): Promise<void> {
  const objectRef = activeObjectRef.value;
  const author = meName();
  const body = draftComment.value.trim();
  if (!author) {
    notifyError("请先在审阅台填写署名，再发表评论。");
    return;
  }
  if (!objectRef || !body || commentBusy.value) return;
  commentBusy.value = true;
  try {
    await apiPost(`/projects/${currentProject()}/collab/comments`, {
      object_ref: objectRef,
      author,
      body,
    });
    if (activeObjectRef.value === objectRef) {
      if (draftComment.value.trim() === body) draftComment.value = "";
      await loadCollab(objectRef);
    }
  } catch (error) {
    if (activeObjectRef.value === objectRef) notifyError(error);
  } finally {
    commentBusy.value = false;
  }
}

function formatCollabTime(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

const profileRows = computed(() => {
  const profile = detailEntity.value?.metadata?.profile;
  if (!profile || typeof profile !== "object") return [];
  return Object.entries(profile as Record<string, unknown>)
    .filter(([, value]) => value != null && value !== "")
    .map(([key, value]) => ({ label: PROFILE_LABELS[key] ?? key, value: String(value) }));
});

function rubricElementText(value: unknown): string {
  if (!Array.isArray(value)) return "";
  return value
    .map((element) => (
      element && typeof element === "object"
        ? String((element as Record<string, unknown>).text ?? "")
        : ""
    ))
    .filter(Boolean)
    .join("；");
}

function detailValues(item: ArchiveItem): DetailRow[] {
  const row = item.raw;
  const fields: Record<ArchiveKind, Array<[string, unknown]>> = {
    entity: [],
    quest: [
      ["目标", row.objective], ["委托人", row.giver_npc], ["地点", row.location],
      ["阶段数", row.stages], ["时间顺序", row.timeline_order], ["状态", row.review_status],
    ],
    region: [
      ["等级范围", `${row.level_min ?? "?"}–${row.level_max ?? "?"}`],
      ["主题", row.themes], ["禁用内容", row.banned_content],
    ],
    poi: [
      ["所属区域", row.region_id], ["用途", row.purpose], ["控制势力", row.controlling_faction],
    ],
    term: [["标准写法", row.canonical], ["别名", row.aliases], ["禁用写法", row.forbidden], ["说明", row.description]],
    dialogue: [
      ["文本键", row.text_key], ["说话人", row.speaker_id], ["关联任务", row.quest_id],
      ["台词", row.text],
      ["父对话树", item.dialogueParent?.treeId], ["树节点", item.dialogueParent?.nodeId],
    ],
    dialogue_tree: [
      ["关联任务", row.quest_id], ["参与者", row.participants], ["节点数", row.nodes],
    ],
    relation: [
      ["源对象", row.source], ["关系类型", row.kind], ["目标对象", row.target],
      ["生效顺序", row.valid_from], ["失效顺序", row.valid_until], ["状态", row.review_status],
    ],
    style_guide: [
      ["指南 ID", row.id], ["正文", row.body],
      ["规则", Array.isArray(row.rules) ? row.rules.join("；") : row.rules],
      ["状态", row.review_status],
    ],
    quest_event_ref: [
      ["任务", row.quest_id], ["事件", row.event_id], ["引用语义", row.ref_kind],
      ["备注", row.note], ["状态", row.review_status],
    ],
    style_rubric: [
      ["抽象风格", row.abstract_tag], ["版本", row.version], ["作者", row.author],
      ["冻结时间", row.frozen_at], ["量表项数", row.element_count],
      ["量表项", rubricElementText(row.elements)], ["状态", row.review_status],
    ],
  };
  return fields[item.kind]
    .filter(([, value]) => value !== "" && value !== null && value !== undefined)
    .map(([label, value]) => ({ label, value: String(value) }));
}
const detailRows = computed(() => detail.value ? detailValues(detail.value) : []);
const workspaceLabel = computed(() => {
  if (detail.value?.kind === "quest") return "在时间线中定位";
  if (detail.value?.kind === "dialogue_tree") return "在对话流中打开";
  if (detail.value?.kind === "dialogue" && detail.value.dialogueParent) return "定位父树与节点";
  return "";
});

async function loadAssets(refValue: string, append = false): Promise<void> {
  if (activeObjectRef.value !== refValue) return;
  const cursor = append ? assetNextCursor.value : null;
  if (append && (!cursor || assetsLoadingMore.value)) return;
  const requestSerial = ++assetsRequestSerial;
  if (append) assetsLoadingMore.value = true;
  else {
    assetsLoading.value = true;
    assetsLoadingMore.value = false;
    assets.value = [];
    assetTotal.value = 0;
    assetNextCursor.value = null;
  }
  try {
    const params = new URLSearchParams({
      object_ref: refValue,
      limit: String(ASSET_PAGE_SIZE),
    });
    if (cursor) params.set("cursor", cursor);
    const body = await apiGet<{
      assets: AssetRow[];
      total?: number;
      next_cursor?: string | null;
    }>(
      `/projects/${currentProject()}/assets?${params.toString()}`,
    );
    if (activeObjectRef.value === refValue && assetsRequestSerial === requestSerial) {
      assets.value = append ? [...assets.value, ...body.assets] : body.assets;
      // Compatibility with an older service that returns the complete thread without metadata.
      assetTotal.value = body.total ?? assets.value.length;
      assetNextCursor.value = body.next_cursor ?? null;
    }
  } catch (error) {
    if (activeObjectRef.value === refValue && assetsRequestSerial === requestSerial) {
      if (append && error instanceof ApiError && [409, 422].includes(error.status)) {
        assetsLoadingMore.value = false;
        await loadAssets(refValue);
        return;
      }
      notifyError(error);
    }
  } finally {
    if (activeObjectRef.value === refValue && assetsRequestSerial === requestSerial) {
      if (append) assetsLoadingMore.value = false;
      else assetsLoading.value = false;
    }
  }
}

function resetDetailState(releaseLock = true): void {
  const objectRef = activeObjectRef.value;
  if (releaseLock && objectRef) releaseDetailLock(objectRef);
  detail.value = null;
  assetsRequestSerial += 1;
  assets.value = [];
  assetsLoading.value = false;
  assetsLoadingMore.value = false;
  assetTotal.value = 0;
  assetNextCursor.value = null;
  newAsset.value = { kind: "image", uri: "", title: "" };
  clearCollabView();
}

function openItem(item: ArchiveItem, syncFocus = true): void {
  const previousRef = activeObjectRef.value;
  if (previousRef && previousRef !== item.ref) {
    releaseDetailLock(previousRef);
    clearCollabView();
  }
  detail.value = item;
  currentKind.value = item.kind;
  showRaw.value = false;
  if (item.kind === "entity") {
    const entity = item.raw as unknown as EntityRow;
    edit.value = { name: entity.name, description: entity.description, tags: entity.tags };
  }
  void loadAssets(item.ref);
  void loadCollab(item.ref);
  if (syncFocus) {
    focusRef.value = item.ref;
    void syncRouteState(true);
  }
}

function closeDetail(): void {
  focusRef.value = "";
  resetDetailState();
  void syncRouteState(true);
}

function focusFromRoute(): void {
  const requested = focusRef.value;
  if (!requested) return;
  const kind = kindFromRef(requested);
  if (!kind) return;
  const item = itemsByKind.value[kind].find((candidate) => candidate.id === objectIdFromRef(requested));
  if (!item) return;
  currentKind.value = kind;
  openItem(item, false);
}

function applyRouteState(): void {
  applyingRoute = true;
  const nextKind = routeKind();
  const nextQuery = routeText(route.query.q);
  const scopeChanged = nextKind !== currentKind.value || nextQuery !== query.value;
  focusRef.value = routeText(route.query.focus);
  currentKind.value = nextKind;
  query.value = nextQuery;
  page.value = routePage();
  currentCursor.value = routeCursor();
  if (scopeChanged) cursorTrail.value = [""];
  cursorTrail.value[page.value - 1] = currentCursor.value;
  if (!focusRef.value) {
    resetDetailState();
  }
  applyingRoute = false;
}

watch(() => route.fullPath, () => {
  if (skipNextRouteLoad) {
    skipNextRouteLoad = false;
    return;
  }
  applyRouteState();
  void load();
});

function inventoryForPage(
  kind: ArchiveKind,
  rows: Array<Record<string, unknown>>,
  payloads: Record<string, DialogueTreePayload>,
): ArchiveInventory {
  const result = emptyInventory();
  if (kind === "entity") result.entities = rows as unknown as EntityRow[];
  else if (kind === "quest") result.quests = rows as unknown as QuestRow[];
  else if (kind === "region") result.regions = rows as unknown as RegionRow[];
  else if (kind === "poi") result.pois = rows as unknown as PoiRow[];
  else if (kind === "term") result.terms = rows as unknown as TermRow[];
  else if (kind === "dialogue") result.dialogues = rows as unknown as DialogueRow[];
  else if (kind === "dialogue_tree") result.dialogue_trees = rows as unknown as DialogueTreeRow[];
  else if (kind === "relation") result.relations = rows as unknown as RelationRow[];
  else if (kind === "style_guide") result.style_guides = rows as unknown as StyleGuideRow[];
  else if (kind === "quest_event_ref") {
    result.quest_event_refs = rows as unknown as QuestEventRefRow[];
  } else result.style_rubrics = rows as unknown as StyleRubricRow[];
  result.dialogue_tree_payloads = payloads;
  return result;
}

async function load(): Promise<void> {
  const sequence = ++loadSequence;
  if (loading.value) loading.value = true;
  else pageLoading.value = true;
  loadError.value = "";
  const params = new URLSearchParams({
    kind: currentKind.value,
    limit: String(PAGE_SIZE),
  });
  if (query.value.trim()) params.set("q", query.value.trim());
  if (currentCursor.value) params.set("cursor", currentCursor.value);
  if (focusRef.value) params.set("focus", focusRef.value);
  try {
    const body = await apiGet<ArchivePageResponse>(
      `/projects/${currentProject()}/archive?${params.toString()}`,
    );
    if (sequence !== loadSequence) return;
    const responseCounts = body.counts ?? {};
    counts.value = Object.fromEntries(
      ARCHIVE_KINDS.map((kind) => [kind, Number(responseCounts[kind] ?? 0)]),
    ) as Record<ArchiveKind, number>;
    currentTotal.value = body.total;
    nextCursor.value = body.next_cursor;
    previousCursor.value = body.previous_cursor ?? null;
    snapshotEtag.value = body.snapshot_etag;
    currentOffset.value = Math.max(0, body.offset ?? ((page.value - 1) * PAGE_SIZE));
    page.value = Math.floor(currentOffset.value / PAGE_SIZE) + 1;
    if (body.page_cursor !== undefined) currentCursor.value = body.page_cursor ?? "";
    cursorTrail.value[page.value - 1] = currentCursor.value;
    inventory.value = inventoryForPage(
      body.kind,
      body.items,
      body.dialogue_tree_payloads ?? {},
    );
    focusFromRoute();
    await nextTick();
    await syncRouteState(true);
  } catch (error) {
    if (sequence !== loadSequence) return;
    if (currentCursor.value && error instanceof ApiError && [409, 422].includes(error.status)) {
      resetPaging();
      await syncRouteState(true);
      await load();
      return;
    }
    loadError.value = humanizeError(error);
    notifyError(error);
  } finally {
    if (sequence === loadSequence) {
      loading.value = false;
      pageLoading.value = false;
    }
  }
}
onMounted(load);
onBeforeUnmount(() => {
  const objectRef = activeObjectRef.value;
  window.clearTimeout(searchTimer);
  window.clearInterval(lockHeartbeatTimer);
  collabRequestSerial += 1;
  assetsRequestSerial += 1;
  if (objectRef) releaseDetailLock(objectRef);
  detail.value = null;
});

async function openWorkspace(): Promise<void> {
  const item = detail.value;
  if (!item || !workspaceLabel.value) return;
  await router.push(locationForObject(item, inventory.value, query.value));
}

async function attachAsset(): Promise<void> {
  const item = detail.value;
  if (!item || !newAsset.value.uri.trim()) return;
  const lock = await ensureOwnLock(item.ref);
  if (!lock.ok || activeObjectRef.value !== item.ref) return;
  try {
    await apiPost(`/projects/${currentProject()}/assets:attach`, {
      object_ref: item.ref,
      kind: newAsset.value.kind,
      uri: newAsset.value.uri.trim(),
      title: newAsset.value.title.trim(),
    });
    if (activeObjectRef.value !== item.ref) return;
    newAsset.value = { kind: "image", uri: "", title: "" };
    await loadAssets(item.ref);
  } catch (error) {
    notifyError(error);
  }
}

async function detachAsset(id: string): Promise<void> {
  const item = detail.value;
  if (!item) return;
  const lock = await ensureOwnLock(item.ref);
  if (!lock.ok || activeObjectRef.value !== item.ref) return;
  try {
    await apiPost(`/projects/${currentProject()}/assets:detach?asset_id=${encodeURIComponent(id)}`, {});
    if (activeObjectRef.value !== item.ref) return;
    await loadAssets(item.ref);
  } catch (error) {
    notifyError(error);
  }
}

async function save(): Promise<void> {
  const entity = detailEntity.value;
  if (!entity || saving.value) return;
  const objectRef = `entity:${entity.id}`;
  const lock = await ensureOwnLock(objectRef);
  if (!lock.ok || activeObjectRef.value !== objectRef) return;
  saving.value = true;
  try {
    await apiPatch(`/projects/${currentProject()}/entities/${encodeURIComponent(entity.id)}`, {
      name: edit.value.name.trim() || undefined,
      description: edit.value.description,
      tags: edit.value.tags.split(/[,，]/).map((tag) => tag.trim()).filter(Boolean),
    });
    if (activeObjectRef.value !== objectRef) return;
    notifyOk("已保存，手改即时署名入正典。");
    closeDetail();
    await load();
  } catch (error) {
    notifyError(error);
  } finally {
    saving.value = false;
  }
}

async function remove(): Promise<void> {
  const item = detail.value;
  if (!item || !canDeleteDetail.value) return;
  const isEntity = item.kind === "entity";
  const warning = isEntity ? "相关关系会一并清理" : "整棵树及其节点会被移除";
  if (!(await askConfirm(`删除「${item.title}」？${warning}，且不可撤销。`, {
    title: isEntity ? "删除设定实体" : "删除对话树",
    confirmLabel: isEntity ? "删除实体" : "删除整棵树",
  }))) return;
  const lock = await ensureOwnLock(item.ref);
  if (!lock.ok || activeObjectRef.value !== item.ref) return;
  try {
    await apiDelete(
      `/projects/${currentProject()}/objects/${item.kind}/${encodeURIComponent(item.id)}`,
    );
    notifyOk(`已删除「${item.title}」。`);
    if (activeObjectRef.value === item.ref) closeDetail();
    await load();
  } catch (error) {
    notifyError(error);
  }
}
</script>

<template>
  <section>
    <PageHead
      overline="ARCHIVE"
      title="设定档案"
      purpose="按对象类型浏览正典，搜索后可复制深链或进入对应编辑视图。"
    />

    <div v-if="!loading && !loadError && totalItems" class="archive-controls">
      <div class="kind-tabs" role="tablist" aria-label="档案对象类型">
        <button
          v-for="config in KIND_CONFIGS"
          :key="config.kind"
          type="button"
          role="tab"
          :disabled="pageLoading"
          :aria-selected="currentKind === config.kind"
          :class="{ active: currentKind === config.kind }"
          @click="selectKind(config.kind)"
        >
          <span>{{ config.label }}</span>
          <b>{{ counts[config.kind] }}</b>
        </button>
      </div>
      <div class="toolbar">
        <label class="search-wrap">
          <span class="sr-only">搜索当前对象类型</span>
          <input
            v-model="query"
            class="search"
            :placeholder="`搜索${currentConfig.label}的名称 / ID / 正文…`"
          />
        </label>
        <button v-if="query" type="button" class="ghost clear" @click="clearSearch">清除</button>
        <span v-if="pageLoading" class="muted count" role="status">读取当前页…</span>
        <span class="muted count">{{ rangeLabel }}</span>
      </div>
    </div>

    <AsyncState v-if="loading" state="loading" title="正在展开设定档案…" compact />
    <AsyncState
      v-else-if="loadError"
      state="error"
      title="设定档案读取失败"
      :hint="loadError"
      action-label="重新加载"
      compact
      @action="load"
    />
    <AsyncState
      v-else-if="!totalItems"
      state="empty"
      title="设定档案还是空的"
      hint="从文稿提炼、创世或人物工坊录入内容后，正典对象会出现在这里。"
      compact
    />
    <AsyncState
      v-else-if="!currentTotal && !query"
      state="empty"
      :title="currentConfig.empty"
      hint="可以切换上方对象类型继续浏览。"
      compact
    />
    <div v-else class="pane tablewrap" :aria-busy="pageLoading || undefined">
      <table>
        <thead>
          <tr>
            <th>{{ currentConfig.label }}</th>
            <th>ID</th>
            <th>摘要</th>
            <th>线索</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="item in pagedItems"
            :key="item.ref"
            class="row"
            role="button"
            tabindex="0"
            @click="openItem(item)"
            @keydown.enter="openItem(item)"
            @keydown.space.prevent="openItem(item)"
          >
            <td class="name">{{ item.title }}</td>
            <td class="mono">{{ item.id }}</td>
            <td class="muted desc">{{ item.summary || "—" }}</td>
            <td class="muted meta">{{ item.meta || "—" }}</td>
          </tr>
          <tr v-if="!currentItems.length">
            <td colspan="4" class="muted empty">
              <div class="filter-empty">
                <span>没有匹配“{{ query }}”的{{ currentConfig.label }}。</span>
                <button type="button" class="ghost" @click="clearSearch">清除搜索</button>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <nav v-if="pageCount > 1" class="pagination" aria-label="档案分页">
        <button type="button" class="ghost" :disabled="!canGoPrevious || pageLoading" @click="goPrevious">上一页</button>
        <span><b>{{ page }}</b> / {{ pageCount }}</span>
        <button type="button" class="ghost" :disabled="!nextCursor || pageLoading" @click="goNext">下一页</button>
      </nav>
    </div>

    <Modal
      :open="detail !== null"
      :overline="detail ? KIND_LABELS[detail.kind] : ''"
      :title="detail?.title ?? ''"
      @close="closeDetail"
    >
      <div v-if="detail" class="d-form">
        <div class="d-meta">
          <span class="type-chip">{{ KIND_LABELS[detail.kind] }}</span>
          <span class="mono">{{ detail.ref }}</span>
        </div>

        <template v-if="detailEntity">
          <label class="f"><span>名称</span><input v-model="edit.name" maxlength="120" :disabled="collabLoading || lockedByOther" /></label>
          <label class="f"><span>描述</span><textarea v-model="edit.description" rows="4" :disabled="collabLoading || lockedByOther"></textarea></label>
          <label class="f">
            <span>标签<i class="muted">（逗号分隔）</i></span>
            <input v-model="edit.tags" placeholder="例如：港口, 走私, 中立" :disabled="collabLoading || lockedByOther" />
          </label>
          <template v-if="profileRows.length">
            <div class="section sub"><span class="t">角色卡</span></div>
            <dl class="d-rows">
              <template v-for="row in profileRows" :key="row.label">
                <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
              </template>
            </dl>
          </template>

        </template>

        <template v-else>
          <dl class="object-detail">
            <template v-for="row in detailRows" :key="row.label">
              <dt>{{ row.label }}</dt><dd>{{ row.value }}</dd>
            </template>
          </dl>
          <p v-if="detail.kind === 'dialogue' && !detail.dialogueParent" class="parent-missing">
            这条独立对话未匹配到对话树节点，仍可在此查看其完整记录。
          </p>
        </template>

        <div class="section sub collab-title"><span class="t">团队协作</span></div>
        <div
          class="collab-panel"
          data-collab-object-detail
          :data-collab-snapshot="collabSnapshotEtag || undefined"
          :aria-busy="collabLoading || collabLoadingMore || undefined"
        >
          <div class="collab-line">
            <span class="collab-label">负责人</span>
            <span v-if="collab.assignment" class="assignee">@{{ collab.assignment.assignee }}</span>
            <span v-else class="muted">未指派</span>
            <span class="spacer"></span>
            <button
              type="button"
              class="ghost xs"
              :disabled="collabLoading || assignmentBusy"
              @click="assignToMe(false)"
            >{{ collab.assignment?.assignee === meName() ? "已由我负责" : "指派给我" }}</button>
            <button
              v-if="collab.assignment"
              type="button"
              class="ghost xs"
              :disabled="collabLoading || assignmentBusy"
              @click="assignToMe(true)"
            >取消指派</button>
          </div>
          <div class="collab-line lock-line">
            <span class="collab-label">状态</span>
            <span
              class="collab-state"
              :class="{ mine: lockHeldByMe, blocked: lockedByOther }"
              aria-live="polite"
            >{{ collaborationStatus }}</span>
            <span v-if="collab.lock?.at" class="muted lock-time">
              {{ formatCollabTime(collab.lock.at) }}
            </span>
            <span class="spacer"></span>
            <button
              type="button"
              class="ghost xs"
              :disabled="collabLoading || lockBusy || lockedByOther"
              @click="setEditLock(lockHeldByMe)"
            >{{ lockBusy ? "处理中…" : lockHeldByMe ? "释放编辑锁" : "锁定编辑" }}</button>
            <button
              type="button"
              class="ghost xs"
              :disabled="collabLoading"
              @click="loadCollab()"
            >刷新</button>
          </div>
          <p v-if="lockedByOther" class="lock-warning">
            @{{ collab.lock?.holder }} 正在编辑；当前详情与素材操作保持只读。
          </p>

          <div class="comment-thread">
            <div v-for="comment in collab.comments" :key="comment.id" class="comment-row">
              <span class="comment-author">@{{ comment.author }}</span>
              <span class="comment-body">{{ comment.body }}</span>
              <time :datetime="comment.at" class="muted">{{ formatCollabTime(comment.at) }}</time>
            </div>
            <p v-if="!collabLoading && !collab.comments.length" class="muted no-comments">
              还没有评论。
            </p>
            <div v-if="collabTotal" class="comment-page-status">
              <span class="muted">已显示 {{ collab.comments.length }} / {{ collabTotal }} 条（最新在前）</span>
              <button
                v-if="collabNextCursor"
                type="button"
                class="ghost xs"
                :disabled="collabLoading || collabLoadingMore"
                @click="loadCollab(activeObjectRef, true)"
              >{{ collabLoadingMore ? "读取中…" : "加载更早评论" }}</button>
            </div>
          </div>
          <div class="comment-add">
            <input
              v-model="draftComment"
              maxlength="2000"
              placeholder="给这个正典对象留个评论…"
              aria-label="协作评论"
              @keydown.enter.prevent="postComment"
            />
            <button
              type="button"
              class="ghost xs"
              :disabled="commentBusy || !draftComment.trim()"
              @click="postComment"
            >{{ commentBusy ? "提交中…" : "评论" }}</button>
          </div>
          <p v-if="!meName()" class="muted operator-hint">认领、评论和锁定前，请先在审阅台填写署名。</p>
        </div>

        <div class="section sub"><span class="t">关联素材</span></div>
        <p class="muted asset-hint">挂接已有的概念图 / 音频 / 地图 / 链接（填路径或 URL，不生成图）。</p>
        <p v-if="assetsLoading" class="muted asset-status" role="status">正在读取素材…</p>
        <div v-if="assets.length" class="asset-list">
          <div v-for="asset in assets" :key="asset.id" class="asset-row">
            <span class="asset-kind">{{ ASSET_KIND_LABELS[asset.kind] ?? asset.kind }}</span>
            <span class="asset-title">{{ asset.title || asset.uri }}</span>
            <span class="mono asset-uri">{{ asset.uri }}</span>
            <button
              class="asset-x"
              type="button"
              :aria-label="`解除素材：${asset.title || asset.uri}`"
              :disabled="collabLoading || lockBusy || lockedByOther"
              @click="detachAsset(asset.id)"
            >×</button>
          </div>
          <div class="asset-page-meta">
            <span>已加载 {{ assets.length }} / {{ assetTotal }}</span>
            <button
              v-if="assetNextCursor"
              type="button"
              class="ghost xs"
              :disabled="assetsLoadingMore"
              @click="loadAssets(activeObjectRef, true)"
            >{{ assetsLoadingMore ? "读取中…" : "加载更多素材" }}</button>
          </div>
        </div>
        <div class="asset-add">
          <select v-model="newAsset.kind" class="sel" aria-label="素材类型" :disabled="collabLoading || lockedByOther">
            <option value="image">图像</option><option value="audio">音频</option>
            <option value="map">地图</option><option value="link">链接</option>
          </select>
          <input v-model="newAsset.uri" aria-label="素材路径或 URL" placeholder="art/x.png 或 https://…" :disabled="collabLoading || lockedByOther" />
          <input v-model="newAsset.title" aria-label="素材标题" placeholder="标题（可选）" class="asset-title-in" :disabled="collabLoading || lockedByOther" />
          <button class="ghost" type="button" :disabled="!newAsset.uri.trim() || collabLoading || lockBusy || lockedByOther" @click="attachAsset">挂接</button>
        </div>

        <button class="rawtoggle" type="button" @click="showRaw = !showRaw">
          {{ showRaw ? "收起原始数据" : "查看原始数据" }}
        </button>
        <pre v-if="showRaw" class="raw">{{ JSON.stringify(detail.raw, null, 2) }}</pre>
      </div>
      <template #footer>
        <button v-if="canDeleteDetail" class="ghost danger" :disabled="collabLoading || lockBusy || lockedByOther" @click="remove">
          {{ detail?.kind === "dialogue_tree" ? "删除整棵树" : "删除" }}
        </button>
        <span class="spacer"></span>
        <button class="ghost" @click="closeDetail">关闭</button>
        <button v-if="workspaceLabel" class="ghost destination" @click="openWorkspace">{{ workspaceLabel }}</button>
        <button v-if="detailEntity" class="primary" :disabled="saving || collabLoading || lockBusy || lockedByOther" @click="save">
          {{ saving ? "保存中…" : "保存" }}
        </button>
      </template>
    </Modal>
  </section>
</template>

<style scoped>
.archive-controls { margin-bottom: 0.75rem; }
.kind-tabs {
  display: flex;
  gap: 0.35rem;
  overflow-x: auto;
  padding: 0.1rem 0 0.55rem;
  scrollbar-width: thin;
}
.kind-tabs button {
  min-height: 38px;
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  flex: none;
  padding: 0.38rem 0.65rem;
  border: 1px solid var(--ow-line);
  border-radius: 2px;
  color: var(--ow-muted);
  background: var(--ow-panel-2);
  font: inherit;
  font-size: 0.8rem;
  cursor: pointer;
}
.kind-tabs button b {
  min-width: 1.35rem;
  padding: 0.05rem 0.28rem;
  color: var(--ow-cyan);
  background: rgba(143, 214, 232, 0.08);
  font: 0.68rem ui-monospace, Consolas, monospace;
}
.kind-tabs button.active {
  border-color: var(--ow-gold-soft);
  color: var(--ow-gold-bright);
  background: var(--ow-gold-faint);
  box-shadow: inset 0 -2px var(--ow-gold);
}
.toolbar { display: flex; gap: 0.5rem; align-items: center; }
.search-wrap { flex: 1; min-width: 10rem; }
.search {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.7rem;
  font: inherit;
}
.count { font-size: 0.78rem; white-space: nowrap; }
.tablewrap { overflow-x: auto; padding: 0.2rem 0.6rem 0.6rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.86rem; }
th { text-align: left; color: var(--ow-muted); font-weight: 500; padding: 0.5rem 0.6rem; border-bottom: 1px solid var(--ow-line); }
td { padding: 0.5rem 0.6rem; border-bottom: 1px solid rgba(46, 54, 88, 0.55); vertical-align: top; }
.row { cursor: pointer; transition: background 0.12s ease; }
.row:hover, .row:focus-visible { background: rgba(143, 214, 232, 0.06); outline: none; }
.name { min-width: 9rem; color: var(--ow-ink); font-weight: 600; }
.mono { font-family: ui-monospace, Consolas, monospace; color: var(--ow-cyan); font-size: 0.78rem; }
.desc { min-width: 12rem; max-width: 30rem; }
.meta { min-width: 9rem; }
.empty { text-align: center; padding: 1.5rem; }
.filter-empty { display: flex; align-items: center; justify-content: center; flex-wrap: wrap; gap: 0.65rem; }
.pagination { display: flex; align-items: center; justify-content: center; gap: 0.7rem; padding-top: 0.7rem; }
.pagination span { color: var(--ow-muted); font-size: 0.78rem; }
.pagination b { color: var(--ow-gold-bright); }
.d-form { display: flex; flex-direction: column; gap: 0.7rem; }
.d-meta { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; }
.type-chip, .asset-kind {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  padding: 0.06rem 0.5rem;
  color: var(--ow-cyan);
  font-size: 0.74rem;
}
.f { display: flex; flex-direction: column; gap: 0.3rem; }
.f span { color: var(--ow-muted); font-size: 0.8rem; }
.f input, .f textarea, .asset-add input, .sel {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.86rem;
}
.f textarea { resize: vertical; }
.d-rows, .object-detail { display: grid; grid-template-columns: minmax(6rem, max-content) 1fr; gap: 0.42rem 0.9rem; margin: 0; }
.d-rows dt, .object-detail dt { color: var(--ow-muted); font-size: 0.78rem; }
.d-rows dd, .object-detail dd { margin: 0; color: var(--ow-ink); line-height: 1.55; overflow-wrap: anywhere; }
.parent-missing { margin: 0; padding: 0.6rem 0.7rem; border-left: 2px solid var(--ow-gold-soft); color: var(--ow-muted); font-size: 0.8rem; }
.collab-title { margin-top: 0.1rem; }
.collab-panel {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  padding: 0.65rem;
  border: 1px solid var(--ow-line);
  background: rgba(12, 18, 37, 0.45);
}
.collab-line { display: flex; align-items: center; flex-wrap: wrap; gap: 0.4rem; font-size: 0.78rem; }
.collab-label { min-width: 3.4rem; color: var(--ow-muted); }
.assignee { color: var(--ow-gold-bright); }
.collab-state {
  padding: 0.1rem 0.45rem;
  border: 1px solid var(--ow-line);
  color: var(--ow-cyan);
  background: rgba(143, 214, 232, 0.05);
}
.collab-state.mine { border-color: rgba(142, 212, 172, 0.4); color: #8ed4ac; }
.collab-state.blocked { border-color: rgba(224, 133, 133, 0.45); color: #e89a9a; }
.lock-time { font-size: 0.7rem; }
.lock-warning { margin: 0; padding: 0.35rem 0.55rem; border-left: 2px solid #e89a9a; color: #e89a9a; font-size: 0.76rem; }
.comment-thread { display: flex; flex-direction: column; gap: 0.3rem; }
.comment-row {
  display: grid;
  grid-template-columns: minmax(5rem, max-content) 1fr max-content;
  gap: 0.5rem;
  align-items: baseline;
  padding: 0.35rem 0.5rem;
  background: var(--ow-panel-2);
  border: 1px solid rgba(46, 54, 88, 0.65);
  font-size: 0.78rem;
}
.comment-author { color: var(--ow-cyan); }
.comment-body { min-width: 0; overflow-wrap: anywhere; }
.comment-row time { font-size: 0.68rem; white-space: nowrap; }
.no-comments { margin: 0.1rem 0; font-size: 0.76rem; }
.comment-page-status {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-size: 0.72rem;
}
.comment-add { display: flex; gap: 0.4rem; }
.comment-add input {
  flex: 1;
  min-width: 8rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.4rem 0.55rem;
  font: inherit;
  font-size: 0.8rem;
}
.operator-hint { margin: 0; font-size: 0.74rem; }
.xs { padding: 0.2rem 0.5rem; font-size: 0.72rem; }
.asset-hint { margin: -0.3rem 0 0; font-size: 0.78rem; }
.asset-status { margin: 0.45rem 0; font-size: 0.76rem; }
.asset-list { display: flex; flex-direction: column; gap: 0.35rem; }
.asset-row { display: flex; align-items: baseline; gap: 0.55rem; padding: 0.35rem 0.6rem; border: 1px solid var(--ow-line); background: var(--ow-panel-2); font-size: 0.82rem; }
.asset-page-meta { display: flex; align-items: center; justify-content: space-between; gap: 0.7rem; color: var(--ow-muted); font-size: 0.74rem; }
.asset-title { color: var(--ow-ink); }
.asset-uri { margin-left: auto; max-width: 14rem; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.asset-x { border: 0; background: transparent; color: var(--ow-muted); font-size: 1.1rem; cursor: pointer; }
.asset-add { display: flex; gap: 0.4rem; flex-wrap: wrap; }
.asset-add input { flex: 1; min-width: 8rem; }
.asset-title-in { max-width: 9rem; }
.rawtoggle { align-self: flex-start; }
.raw { margin: 0; max-height: 260px; overflow: auto; padding: 0.65rem; border: 1px solid var(--ow-line); background: var(--ow-night-deep); color: var(--ow-cyan); font: 0.74rem/1.5 ui-monospace, Consolas, monospace; }
button {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.45rem 0.9rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.84rem;
}
button.primary { background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%); border-color: rgba(240, 210, 138, 0.65); color: #241a05; font-weight: 600; }
button:disabled { opacity: 0.5; cursor: not-allowed; }
button.ghost:hover, button.destination { border-color: var(--ow-gold-soft); color: var(--ow-gold-bright); }
button.danger { color: #e89a9a; }
.spacer { flex: 1; }
.sr-only { position: absolute; width: 1px; height: 1px; padding: 0; margin: -1px; overflow: hidden; clip: rect(0, 0, 0, 0); white-space: nowrap; border: 0; }

@media (max-width: 600px) {
  .toolbar { align-items: stretch; flex-wrap: wrap; }
  .search-wrap { flex-basis: 100%; }
  .search, button, .f input, .asset-add input, .sel { min-height: 44px; }
  .count { margin-left: auto; align-self: center; }
  .tablewrap { padding-inline: 0.25rem; }
  th:nth-child(4), td:nth-child(4) { display: none; }
  .d-rows, .object-detail { grid-template-columns: 1fr; gap: 0.1rem; }
  .d-rows dd, .object-detail dd { margin-bottom: 0.45rem; }
  .collab-line .spacer { display: none; }
  .collab-line button { flex: 1; }
  .comment-row { grid-template-columns: 1fr; gap: 0.15rem; }
  .comment-add { align-items: stretch; flex-direction: column; }
  .asset-row { align-items: flex-start; flex-wrap: wrap; }
  .asset-uri { width: 100%; max-width: none; margin-left: 0; white-space: normal; overflow-wrap: anywhere; }
  .asset-add > * { width: 100%; max-width: none; }
}
</style>
