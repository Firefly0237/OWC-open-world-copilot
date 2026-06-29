<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { apiGet, apiPost, currentOperator, currentProject } from "../api";
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
const router = useRouter();

// safe-rename state (inline panel for a chosen hit)
const renaming = ref<Hit | null>(null);
const newName = ref("");
const newId = ref("");
const plan = ref<RenamePlan | null>(null);
const busy = ref(false);
const done = ref<{ snap: string; edits: number } | null>(null);

const KIND_ROUTE: Record<string, string> = {
  entity: "/archive",
  term: "/archive",
  region: "/archive",
  poi: "/archive",
  localized_text: "/archive",
  quest: "/timeline",
  dialogue: "/dialogues",
  dialogue_tree: "/dialogues",
};
const KIND_LABEL: Record<string, string> = {
  entity: "实体",
  quest: "任务",
  region: "区域",
  poi: "地点",
  term: "词条",
  dialogue: "对话",
  dialogue_tree: "对话树",
  localized_text: "本地化",
};

function show(): void {
  open.value = true;
  nextTick(() => input.value?.focus());
}
function hide(): void {
  open.value = false;
  query.value = "";
  hits.value = [];
  renaming.value = null;
  plan.value = null;
  done.value = null;
}
function onKey(e: KeyboardEvent): void {
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
    e.preventDefault();
    open.value ? hide() : show();
  } else if (e.key === "Escape" && open.value) {
    hide();
  }
}
onMounted(() => window.addEventListener("keydown", onKey));
onUnmounted(() => window.removeEventListener("keydown", onKey));

let timer = 0;
watch(query, (q) => {
  window.clearTimeout(timer);
  active.value = 0;
  if (!q.trim()) {
    hits.value = [];
    return;
  }
  timer = window.setTimeout(async () => {
    try {
      const body = await apiGet<{ hits: Hit[] }>(
        `/projects/${currentProject()}/search?q=${encodeURIComponent(q.trim())}`,
      );
      hits.value = body.hits;
    } catch (e) {
      notifyError(e);
    }
  }, 180);
});

function jump(hit: Hit): void {
  const route = KIND_ROUTE[hit.kind] ?? "/archive";
  hide();
  router.push(route);
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
  busy.value = true;
  try {
    const old = renaming.value.ref.split(":").slice(1).join(":");
    const res = await apiPost<{ undo_snapshot_id: string; post_audit_open_errors: number }>(
      `/projects/${currentProject()}/rename:apply`,
      {
        ref: old,
        operator: currentOperator(),
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

const canApply = computed(() => plan.value !== null && plan.value.conflicts.length === 0);
</script>

<template>
  <Teleport to="body">
    <div v-if="open" class="cp-scrim" @click.self="hide">
      <div class="cp pane">
        <input
          ref="input"
          v-model="query"
          class="cp-input"
          placeholder="全局检索：实体 / 任务 / 对话 / 词条…（Esc 关闭）"
          @keydown.down.prevent="active = Math.min(active + 1, hits.length - 1)"
          @keydown.up.prevent="active = Math.max(active - 1, 0)"
          @keydown.enter.prevent="hits[active] && jump(hits[active])"
        />
        <ul v-if="hits.length && !renaming" class="cp-list">
          <li
            v-for="(hit, i) in hits"
            :key="hit.ref"
            :class="{ on: i === active }"
            @mouseenter="active = i"
            @click="jump(hit)"
          >
            <span class="cp-kind">{{ KIND_LABEL[hit.kind] ?? hit.kind }}</span>
            <span class="cp-title">{{ hit.title }}</span>
            <span class="cp-snip mono">{{ hit.snippet }}</span>
            <button class="cp-rename ghost" @click.stop="startRename(hit)">重命名</button>
          </li>
        </ul>
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
