<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import {
  apiGet,
  apiPost,
  currentOperator,
  currentProject,
  setCurrentOperator,
} from "../api";
import {
  locationForObject,
  type ArchiveNavigationInventory,
  type DialogueInventoryRow,
  type DialogueTreePayload,
} from "../objectNavigation";
import { notifyError, notifyOk } from "../toast";

interface Hit {
  ref: string;
  kind: string;
  title: string;
  snippet: string;
  score: number;
}
interface RefEdit {
  owner_ref: string;
  field: string;
}
interface RenamePlan {
  target: string;
  old_id: string;
  edits: RefEdit[];
  conflicts: string[];
}

const open = ref(false);
const query = ref("");
const hits = ref<Hit[]>([]);
const active = ref(0);
const input = ref<HTMLInputElement | null>(null);
const dialog = ref<HTMLElement | null>(null);
const searching = ref(false);
const router = useRouter();
let returnFocus: HTMLElement | null = null;
const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

// safe-rename state (inline panel for a chosen hit)
const renaming = ref<Hit | null>(null);
const newName = ref("");
const newId = ref("");
const plan = ref<RenamePlan | null>(null);
const busy = ref(false);
const done = ref<{ snap: string; edits: number } | null>(null);
const operator = ref(currentOperator());

const KIND_LABEL: Record<string, string> = {
  entity: "实体",
  quest: "任务",
  region: "区域",
  poi: "地点",
  term: "词条",
  dialogue: "对话",
  dialogue_tree: "对话树",
  localized_text: "本地化",
  relation: "关系",
  style_guide: "风格圣经",
  quest_event_ref: "任务事件引用",
  style_rubric: "风格量表",
};

const RENAMEABLE_KINDS = new Set([
  "entity",
  "quest",
  "region",
  "poi",
  "term",
  "dialogue",
  "dialogue_tree",
  "localized_text",
]);

function canRename(hit: Hit): boolean {
  return RENAMEABLE_KINDS.has(hit.kind);
}

function show(): void {
  returnFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  open.value = true;
  nextTick(() => input.value?.focus());
}
defineExpose({ show });
function hide(): void {
  window.clearTimeout(timer);
  requestSerial += 1;
  open.value = false;
  query.value = "";
  hits.value = [];
  renaming.value = null;
  plan.value = null;
  done.value = null;
  searching.value = false;
  const target = returnFocus;
  returnFocus = null;
  void nextTick(() => target?.focus());
}
function onKey(e: KeyboardEvent): void {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    open.value ? hide() : show();
  } else if (e.key === "Escape" && open.value) {
    hide();
  } else if (e.key === "Tab" && open.value) {
    const focusable = Array.from(dialog.value?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? []);
    if (!focusable.length) {
      e.preventDefault();
      dialog.value?.focus();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first.focus();
    }
  }
}
onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => {
  window.removeEventListener("keydown", onKey);
  window.clearTimeout(timer);
  requestSerial += 1;
});

let timer = 0;
let requestSerial = 0;
watch(query, (q) => {
  window.clearTimeout(timer);
  const serial = ++requestSerial;
  active.value = 0;
  if (!q.trim()) {
    hits.value = [];
    searching.value = false;
    return;
  }
  searching.value = true;
  timer = window.setTimeout(async () => {
    try {
      const body = await apiGet<{ hits: Hit[] }>(
        `/projects/${currentProject()}/search?q=${encodeURIComponent(q.trim())}`,
      );
      if (serial === requestSerial) hits.value = body.hits;
    } catch (e) {
      if (serial === requestSerial) notifyError(e);
    } finally {
      if (serial === requestSerial) searching.value = false;
    }
  }, 180);
});

async function navigationInventory(dialogueRef: string): Promise<ArchiveNavigationInventory> {
  const params = new URLSearchParams({ kind: "dialogue", focus: dialogueRef, limit: "1" });
  const body = await apiGet<{
    items: DialogueInventoryRow[];
    dialogue_tree_payloads?: Record<string, DialogueTreePayload>;
  }>(`/projects/${currentProject()}/archive?${params.toString()}`);
  return {
    dialogues: body.items,
    dialogue_tree_payloads: body.dialogue_tree_payloads ?? {},
  };
}

async function jump(hit: Hit): Promise<void> {
  let inventory: ArchiveNavigationInventory = {};
  if (hit.kind === "dialogue") {
    try {
      inventory = await navigationInventory(hit.ref);
    } catch {
      // Parent lookup is an enhancement; Archive remains an honest fallback when it is unavailable.
    }
  }
  const location = locationForObject(hit, inventory, query.value);
  hide();
  await router.push(location);
}

function startRename(hit: Hit): void {
  renaming.value = hit;
  newName.value = hit.title;
  newId.value = hit.ref.split(":").slice(1).join(":");
  plan.value = null;
}

async function preview(): Promise<void> {
  if (!renaming.value) return;
  busy.value = true;
  try {
    const old = renaming.value.ref.split(":").slice(1).join(":");
    const body = await apiPost<{ plan: RenamePlan }>(
      `/projects/${currentProject()}/rename:plan`,
      { ref: old, new_name: newName.value || null, new_id: newId.value !== old ? newId.value : null },
    );
    plan.value = body.plan;
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

async function applyRename(): Promise<void> {
  if (!renaming.value || !plan.value || plan.value.conflicts.length) return;
  const signedBy = operator.value.trim();
  if (!signedBy) {
    notifyError("请先填写本次修改的署名。");
    return;
  }
  busy.value = true;
  try {
    setCurrentOperator(signedBy);
    const old = renaming.value.ref.split(":").slice(1).join(":");
    const res = await apiPost<{ undo_snapshot_id: string; post_audit_open_errors: number }>(
      `/projects/${currentProject()}/rename:apply`,
      {
        ref: old,
        operator: signedBy,
        new_name: newName.value || null,
        new_id: newId.value !== old ? newId.value : null,
      },
    );
    done.value = { snap: res.undo_snapshot_id, edits: plan.value.edits.length };
    renaming.value = null;
    plan.value = null;
    notifyOk("已重命名并同步所有引用。");
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

async function undo(): Promise<void> {
  if (!done.value) return;
  busy.value = true;
  try {
    await apiPost(`/projects/${currentProject()}/snapshots:restore`, {
      snapshot_id: done.value.snap,
    });
    notifyOk("已撤销重命名。");
    done.value = null;
  } catch (e) {
    notifyError(e);
  } finally {
    busy.value = false;
  }
}

const canApply = computed(
  () => plan.value !== null && plan.value.conflicts.length === 0 && operator.value.trim().length > 0,
);
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cp-scrim" @click.self="hide">
      <div
        ref="dialog"
        class="cp pane"
        role="dialog"
        aria-modal="true"
        aria-label="全局检索与安全重命名"
        tabindex="-1"
      >
        <input
          ref="input"
          v-model="query"
          class="cp-input"
          placeholder="全局检索：实体 / 任务 / 对话 / 词条…（Esc 关闭）"
          role="combobox"
          aria-controls="global-search-results"
          :aria-expanded="hits.length > 0"
          :aria-activedescendant="hits[active] ? `global-hit-${active}` : undefined"
          @keydown.down.prevent="active = Math.min(active + 1, hits.length - 1)"
          @keydown.up.prevent="active = Math.max(active - 1, 0)"
          @keydown.enter.prevent="hits[active] && jump(hits[active])"
        />
        <ul v-if="hits.length && !renaming" id="global-search-results" class="cp-list" role="listbox">
          <li
            v-for="(hit, i) in hits"
            :key="hit.ref"
            :id="`global-hit-${i}`"
            :class="{ on: i === active }"
            role="option"
            :aria-selected="i === active"
            @mouseenter="active = i"
            @click="jump(hit)"
          >
            <span class="cp-kind">{{ KIND_LABEL[hit.kind] ?? hit.kind }}</span>
            <span class="cp-title">{{ hit.title }}</span>
            <span class="cp-snip mono">{{ hit.snippet }}</span>
            <button v-if="canRename(hit)" class="cp-rename ghost" @click.stop="startRename(hit)">重命名</button>
          </li>
        </ul>
        <p v-else-if="searching && !renaming" class="muted cp-empty" role="status">正在检索…</p>
        <p v-else-if="query.trim() && !renaming" class="muted cp-empty">没有匹配项。</p>

        <div v-if="done" class="cp-done">
          <p class="muted">已重命名并同步 {{ done.edits }} 处引用。</p>
          <div class="cp-rn-actions">
            <button class="ghost" @click="undo" :disabled="busy">撤销</button>
            <button class="apply" @click="hide">完成</button>
          </div>
        </div>

        <div v-if="renaming" class="cp-rn">
          <div class="cp-rn-head">
            <span class="cp-kind">{{ KIND_LABEL[renaming.kind] ?? renaming.kind }}</span>
            安全重命名 <span class="mono">{{ renaming.ref }}</span>
          </div>
          <label>显示名 <input v-model="newName" /></label>
          <label>ID（改 id 会同步所有引用）<input v-model="newId" class="mono" /></label>
          <label>修改署名 <input v-model="operator" autocomplete="name" placeholder="例如：主笔 / Lin" /></label>
          <div class="cp-rn-actions">
            <button class="ghost" @click="preview" :disabled="busy">预览影响</button>
            <button class="ghost" @click="renaming = null">返回</button>
            <button class="apply" @click="applyRename" :disabled="!canApply || busy">应用</button>
          </div>
          <div v-if="plan" class="cp-plan">
            <p v-if="plan.conflicts.length" class="cp-conflict">冲突：{{ plan.conflicts.join("；") }}</p>
            <p v-else class="muted">将同步 {{ plan.edits.length }} 处引用。</p>
            <span v-for="e in plan.edits" :key="e.owner_ref + e.field" class="chip mono">
              {{ e.owner_ref }}·{{ e.field }}
            </span>
          </div>
        </div>
      </div>
    </div>
  </Teleport>
</template>

<style scoped>
.cp-scrim {
  position: fixed;
  inset: 0;
  background: rgba(2, 6, 20, 0.5);
  display: flex;
  justify-content: center;
  align-items: flex-start;
  padding-top: 12vh;
  z-index: 2000;
}
.cp {
  width: min(92vw, 640px);
  padding: 0.75rem;
  max-height: 70vh;
  overflow: auto;
}
.cp-input {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.6rem 0.8rem;
  font-size: 0.95rem;
}
.cp-list {
  list-style: none;
  margin: 0.6rem 0 0;
  padding: 0;
}
.cp-list li {
  display: flex;
  align-items: center;
  gap: 0.6rem;
  padding: 0.45rem 0.5rem;
  border-radius: 0.4rem;
  cursor: pointer;
}
.cp-list li.on {
  background: var(--ow-panel-2);
}
.cp-kind {
  font-size: 0.7rem;
  color: var(--ow-ink-dim);
  border: 1px solid var(--ow-line);
  border-radius: 0.3rem;
  padding: 0.05rem 0.35rem;
  white-space: nowrap;
}
.cp-title {
  font-weight: 600;
  white-space: nowrap;
}
.cp-snip {
  font-size: 0.76rem;
  color: var(--ow-ink-dim);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  flex: 1;
}
.cp-rename {
  font-size: 0.72rem;
  padding: 0.15rem 0.45rem;
}
.cp-empty {
  padding: 0.8rem 0.5rem;
}
.cp-rn {
  padding: 0.4rem 0.3rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}
.cp-rn-head {
  display: flex;
  align-items: center;
  gap: 0.4rem;
  font-size: 0.85rem;
}
.cp-rn label {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.78rem;
  color: var(--ow-ink-dim);
}
.cp-rn input {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.4rem;
  color: var(--ow-ink);
  padding: 0.4rem 0.6rem;
}
.cp-rn-actions {
  display: flex;
  gap: 0.5rem;
}
.cp-rn-actions .apply {
  margin-left: auto;
  background: var(--ow-gold, #d8b46a);
  color: #1a1406;
  border: none;
  border-radius: 0.4rem;
  padding: 0.35rem 0.9rem;
  cursor: pointer;
}
.cp-rn-actions .apply:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.cp-plan {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  align-items: center;
}
.cp-conflict {
  color: var(--ow-flag, #e0653a);
  font-size: 0.8rem;
}
.cp-plan .chip {
  font-size: 0.7rem;
  padding: 0.15rem 0.4rem;
}
</style>
