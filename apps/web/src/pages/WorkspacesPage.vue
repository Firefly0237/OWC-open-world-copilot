<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  ApiError,
  humanizeError,
  apiDownload,
  apiDelete,
  apiGet,
  apiPost,
  currentProject,
  setCurrentProject,
} from "../api";
import PageHead from "../components/PageHead.vue";
import FilePicker from "../components/FilePicker.vue";
import AsyncState from "../components/AsyncState.vue";
import { askConfirm } from "../confirm";

interface WorkspaceInfo {
  name: string;
  path: string;
}

interface WorkspacePage {
  workspaces: WorkspaceInfo[];
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
}

const PAGE_SIZE = 30;
const workspaces = ref<WorkspaceInfo[]>([]);
const workspaceTotal = ref(0);
const nextCursor = ref<string | null>(null);
const newName = ref("");
const importName = ref("");
const importFile = ref<File | null>(null);
const flash = ref("");
const error = ref("");
const active = ref(currentProject());
const downloading = ref("");
const listLoading = ref(true);
const listLoadingMore = ref(false);
const listError = ref("");
const creating = ref(false);
const importing = ref(false);
const removing = ref("");
let listLoadSequence = 0;

function workspaceListPath(cursor = ""): string {
  const query = new URLSearchParams({ limit: String(PAGE_SIZE) });
  if (active.value) query.set("focus", active.value);
  if (cursor) query.set("cursor", cursor);
  return `/workspaces?${query.toString()}`;
}

async function loadWorkspaces(cursor = "", append = false): Promise<void> {
  if (append && (listLoading.value || listLoadingMore.value)) return;
  const sequence = ++listLoadSequence;
  if (append) listLoadingMore.value = true;
  else {
    listLoading.value = true;
    listLoadingMore.value = false;
    listError.value = "";
  }
  try {
    const body = await apiGet<WorkspacePage>(workspaceListPath(cursor));
    if (sequence !== listLoadSequence) return;
    const rows = append ? [...workspaces.value, ...body.workspaces] : body.workspaces;
    workspaces.value = [...new Map(rows.map((workspace) => [workspace.name, workspace])).values()];
    workspaceTotal.value = body.total;
    nextCursor.value = body.next_cursor;
  } catch (e) {
    if (sequence !== listLoadSequence) return;
    if (append && e instanceof ApiError && e.status === 409) {
      flash.value = "世界列表刚刚发生变化，已从第一页重新载入。";
      await loadWorkspaces();
      return;
    }
    if (append) error.value = humanizeError(e);
    else listError.value = humanizeError(e);
  } finally {
    if (sequence === listLoadSequence) {
      listLoading.value = false;
      listLoadingMore.value = false;
    }
  }
}

async function loadMoreWorkspaces(): Promise<void> {
  if (!nextCursor.value) return;
  await loadWorkspaces(nextCursor.value, true);
}

onMounted(loadWorkspaces);

async function create(): Promise<void> {
  if (!newName.value.trim() || creating.value) return;
  creating.value = true;
  flash.value = "";
  error.value = "";
  try {
    const created = await apiPost<{ name: string }>("/workspaces", { name: newName.value.trim() });
    newName.value = "";
    switchTo(created.name);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    creating.value = false;
  }
}

function onFile(file: File): void {
  importFile.value = file;
  if (!importName.value.trim()) {
    importName.value = file.name.replace(/\.zip$/i, "");
  }
}

async function importPack(): Promise<void> {
  if (!importFile.value || !importName.value.trim() || importing.value) return;
  importing.value = true;
  flash.value = "";
  error.value = "";
  try {
    const buffer = await importFile.value.arrayBuffer();
    let binary = "";
    const bytes = new Uint8Array(buffer);
    const chunk = 0x8000;
    for (let i = 0; i < bytes.length; i += chunk) {
      binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
    }
    const imported = await apiPost<{ name: string }>("/workspaces:import", {
      name: importName.value.trim(),
      zip_base64: btoa(binary),
    });
    importName.value = "";
    importFile.value = null;
    switchTo(imported.name);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    importing.value = false;
  }
}

function switchTo(name: string): void {
  setCurrentProject(name);
  active.value = name;
  window.location.assign("/creator");
}

async function remove(name: string): Promise<void> {
  // the current world can't be deleted out from under the app — guide the user to switch first
  if (name === active.value) {
    error.value = "不能删除当前打开的世界。先切换到别的世界，再回来删除它。";
    return;
  }
  if (
    !(await askConfirm(`确定删除世界「${name}」？此操作不可撤销，世界包没备份的话内容会永久丢失。`, {
      title: "删除世界",
      confirmLabel: "删除世界",
    }))
  ) {
    return;
  }
  if (removing.value) return;
  removing.value = name;
  flash.value = "";
  error.value = "";
  try {
    await apiDelete<{ deleted: string }>(`/workspaces/${encodeURIComponent(name)}`);
    flash.value = `世界「${name}」已删除。`;
    await loadWorkspaces();
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    removing.value = "";
  }
}

async function downloadPack(name: string): Promise<void> {
  if (downloading.value) return;
  downloading.value = name;
  error.value = "";
  try {
    await apiDownload(`/workspaces/${encodeURIComponent(name)}/pack`, `${name}.zip`);
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    downloading.value = "";
  }
}
</script>

<template>
  <section>
    <PageHead overline="WORLDS" title="工作区" purpose="新建、切换与管理世界；世界包用于备份与交接。" />
    <p v-if="flash" class="flash" role="status">{{ flash }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <div class="grid">
      <div class="pane block">
        <div class="section"><span class="t">新建</span></div>
        <div class="row">
          <input v-model="newName" maxlength="48" placeholder="世界名称" aria-label="世界名称" @keydown.enter="create" />
          <button class="primary" :disabled="creating || !newName.trim()" @click="create">
            {{ creating ? "创建中…" : "创建" }}
          </button>
        </div>
      </div>
      <div class="pane block">
        <div class="section"><span class="t">导入世界包</span></div>
        <FilePicker accept=".zip" hint="选择世界包 .zip，或拖入" @select="onFile" />
        <div class="row">
          <input v-model="importName" maxlength="48" placeholder="导入为…" aria-label="导入后的世界名称" />
          <button class="primary" :disabled="importing || !importFile || !importName.trim()" @click="importPack">
            {{ importing ? "导入中…" : "导入" }}
          </button>
        </div>
      </div>
    </div>

    <div class="section">
      <span class="t">全部世界</span>
      <span v-if="workspaceTotal" class="count">已加载 {{ workspaces.length }} / {{ workspaceTotal }}</span>
    </div>
    <AsyncState
      v-if="listLoading"
      state="loading"
      title="正在读取世界列表"
      hint="本机世界和外部注册世界会显示在这里。"
      compact
    />
    <AsyncState
      v-else-if="listError"
      state="error"
      title="世界列表读取失败"
      :hint="listError"
      action-label="重新读取"
      compact
      @action="loadWorkspaces"
    />
    <AsyncState
      v-else-if="!workspaces.length"
      state="empty"
      title="还没有世界"
      hint="在上方新建一个世界，或导入已有的世界包。"
      compact
    />
    <TransitionGroup v-else name="list" tag="div" class="worlds">
      <div v-for="w in workspaces" :key="w.name" class="pane world" :class="{ on: w.name === active }">
        <b>{{ w.name }}</b>
        <span class="mono path" :title="w.path">本机存储</span>
        <span class="spacer"></span>
        <span v-if="w.name === active" class="badge">当前</span>
        <button v-else class="ghost" @click="switchTo(w.name)">切换</button>
        <button class="ghost link" :disabled="Boolean(downloading)" @click="downloadPack(w.name)">
          {{ downloading === w.name ? "打包中…" : "下载世界包" }}
        </button>
        <button
          v-if="w.name !== active"
          class="ghost danger"
          :disabled="Boolean(removing)"
          title="删除世界（不可撤销）"
          @click="remove(w.name)"
        >
          {{ removing === w.name ? "删除中…" : "删除" }}
        </button>
      </div>
    </TransitionGroup>
    <div v-if="nextCursor && !listLoading && !listError" class="pager">
      <button class="ghost" :disabled="listLoadingMore" @click="loadMoreWorkspaces">
        {{ listLoadingMore ? "加载中…" : `加载更多（剩余 ${Math.max(0, workspaceTotal - workspaces.length)}）` }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.flash {
  color: #8ed4ac;
}

.error {
  color: #e89a9a;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 280px), 1fr));
  gap: 0.8rem;
  margin-bottom: 1rem;
}

.block {
  padding: 0.9rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.row {
  display: flex;
  gap: 0.5rem;
}

input {
  flex: 1;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

input:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

button,
a.link {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
  font-size: 0.85rem;
  padding: 0.45rem 0.9rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  text-decoration: none;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

button.danger {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

button.danger:hover {
  background: rgba(224, 133, 133, 0.12);
}

.worlds {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.count {
  margin-left: auto;
  color: var(--ow-muted);
  font-size: 0.76rem;
}

.pager {
  display: flex;
  justify-content: center;
  padding: 0.9rem 0;
}

.world {
  display: flex;
  align-items: center;
  gap: 0.8rem;
  padding: 0.65rem 1rem;
}

.world.on {
  border-color: var(--ow-gold-soft);
}

.world b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.path {
  font-size: 0.74rem;
  color: var(--ow-muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 40%;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
}

.spacer {
  flex: 1;
}

.badge {
  border: 1px solid var(--ow-gold-soft);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-gold-bright);
  font-size: 0.74rem;
  padding: 0.1rem 0.6rem;
}

.list-enter-active,
.list-move {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

@media (max-width: 640px) {
  .block {
    padding: 0.8rem;
  }

  .row {
    flex-direction: column;
  }

  .row button,
  .row input {
    width: 100%;
    min-height: 44px;
  }

  .world {
    align-items: stretch;
    flex-wrap: wrap;
    gap: 0.55rem;
  }

  .world b,
  .path {
    width: 100%;
    max-width: none;
  }

  .world .spacer {
    display: none;
  }

  .world button {
    min-height: 44px;
    flex: 1 1 9rem;
  }
}
</style>
