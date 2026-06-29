<script setup lang="ts">
import { onMounted, ref } from "vue";
import {
  humanizeError,
  apiDelete,
  apiGet,
  apiPost,
  apiUrl,
  currentProject,
  setCurrentProject,
} from "../api";
import PageHead from "../components/PageHead.vue";
import FilePicker from "../components/FilePicker.vue";

const workspaces = ref<{ name: string; path: string }[]>([]);
const newName = ref("");
const importName = ref("");
const importFile = ref<File | null>(null);
const flash = ref("");
const error = ref("");
const active = ref(currentProject());

async function refresh(): Promise<void> {
  const body = await apiGet<{ workspaces: { name: string; path: string }[] }>("/workspaces");
  workspaces.value = body.workspaces;
}

onMounted(async () => {
  try {
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  }
});

async function create(): Promise<void> {
  if (!newName.value.trim()) return;
  flash.value = "";
  error.value = "";
  try {
    const created = await apiPost<{ name: string }>("/workspaces", { name: newName.value.trim() });
    newName.value = "";
    // first world on a clean install: adopt it immediately so the whole app has a
    // current project (otherwise the data pages would have nothing to open)
    if (!workspaces.value.length) {
      setCurrentProject(created.name);
      window.location.reload();
      return;
    }
    flash.value = `世界「${created.name}」已创建。`;
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  }
}

function onFile(file: File): void {
  importFile.value = file;
  if (!importName.value.trim()) {
    importName.value = file.name.replace(/\.zip$/i, "");
  }
}

async function importPack(): Promise<void> {
  if (!importFile.value || !importName.value.trim()) return;
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
    flash.value = `世界「${imported.name}」已导入。`;
    importName.value = "";
    importFile.value = null;
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  }
}

function switchTo(name: string): void {
  setCurrentProject(name);
  active.value = name;
  window.location.reload();
}

async function remove(name: string): Promise<void> {
  // the current world can't be deleted out from under the app — guide the user to switch first
  if (name === active.value) {
    error.value = "不能删除当前打开的世界。先切换到别的世界，再回来删除它。";
    return;
  }
  if (!window.confirm(`确定删除世界「${name}」？此操作不可撤销，世界包没备份的话内容会永久丢失。`)) {
    return;
  }
  flash.value = "";
  error.value = "";
  try {
    await apiDelete<{ deleted: string }>(`/workspaces/${encodeURIComponent(name)}`);
    flash.value = `世界「${name}」已删除。`;
    await refresh();
  } catch (e) {
    error.value = humanizeError(e);
  }
}
</script>

<template>
  <section>
    <PageHead overline="WORLDS" title="工作区" purpose="新建、切换与管理世界；世界包用于备份与交接。" />
    <p v-if="flash" class="flash">{{ flash }}</p>
    <p v-if="error" class="error">{{ error }}</p>

    <div class="grid">
      <div class="pane block">
        <div class="section"><span class="t">新建</span></div>
        <div class="row">
          <input v-model="newName" maxlength="48" placeholder="世界名称" @keydown.enter="create" />
          <button class="primary" :disabled="!newName.trim()" @click="create">创建</button>
        </div>
      </div>
      <div class="pane block">
        <div class="section"><span class="t">导入世界包</span></div>
        <FilePicker accept=".zip" hint="选择世界包 .zip，或拖入" @select="onFile" />
        <div class="row">
          <input v-model="importName" maxlength="48" placeholder="导入为…" />
          <button class="primary" :disabled="!importFile || !importName.trim()" @click="importPack">
            导入
          </button>
        </div>
      </div>
    </div>

    <div class="section"><span class="t">全部世界</span></div>
    <p v-if="!workspaces.length" class="muted">还没有世界——先创建或导入一个。</p>
    <TransitionGroup name="list" tag="div" class="worlds">
      <div v-for="w in workspaces" :key="w.name" class="pane world" :class="{ on: w.name === active }">
        <b>{{ w.name }}</b>
        <span class="mono path" :title="w.path">本机存储</span>
        <span class="spacer"></span>
        <span v-if="w.name === active" class="badge">当前</span>
        <button v-else class="ghost" @click="switchTo(w.name)">切换</button>
        <a class="ghost link" :href="apiUrl(`/workspaces/${encodeURIComponent(w.name)}/pack`)">
          下载世界包
        </a>
        <button
          v-if="w.name !== active"
          class="ghost danger"
          title="删除世界（不可撤销）"
          @click="remove(w.name)"
        >
          删除
        </button>
      </div>
    </TransitionGroup>
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
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
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
</style>
