<script setup lang="ts">
import { ref } from "vue";
import { apiPost, apiUrl, currentProject } from "../api";

const ENGINES = [
  { value: "generic", label: "通用 JSON（任意管线）" },
  { value: "unity", label: "Unity" },
  { value: "unreal", label: "Unreal" },
];

interface Manifest {
  target_engine: string;
  content_hash: string;
  files: { path: string; kind: string; sha256: string }[];
}

const engine = ref("generic");
const running = ref(false);
const error = ref("");
const outputDir = ref("");
const manifest = ref<Manifest | null>(null);
const project = currentProject();

async function runExport(): Promise<void> {
  running.value = true;
  error.value = "";
  manifest.value = null;
  try {
    const body = await apiPost<{ output_dir: string; manifest: Manifest }>(
      `/projects/${project}/exports`,
      { target_engine: engine.value },
    );
    outputDir.value = body.output_dir;
    manifest.value = body.manifest;
  } catch (e) {
    error.value = String(e);
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">导出 · 交付物</span></div>
    <p class="muted hint">同一份档案，三种出口：给人看的设定集、给引擎吃的数据包、整库备份。</p>

    <div class="grid">
      <div class="pane block">
        <div class="section"><span class="t">世界设定集</span></div>
        <p class="muted small">按档案实时汇编：世界观、阵营、人物、地点、术语表。适合评审与交接。</p>
        <div class="row">
          <a class="ghost link" :href="apiUrl(`/projects/${project}/lorebook?fmt=md`)">下载 .md</a>
          <a class="ghost link" :href="apiUrl(`/projects/${project}/lorebook?fmt=docx`)">下载 .docx</a>
        </div>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">世界包备份</span></div>
        <p class="muted small">整个世界打包成 .zip，可在「工作区」页导入，用于备份或换机。</p>
        <div class="row">
          <a class="ghost link" :href="apiUrl(`/workspaces/${encodeURIComponent(project)}/pack`)">
            下载世界包
          </a>
        </div>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">引擎数据包</span></div>
        <p class="muted small">结构化 JSON + 校验清单，写入项目目录，供引擎管线读取。</p>
        <div class="row">
          <select v-model="engine">
            <option v-for="e in ENGINES" :key="e.value" :value="e.value">{{ e.label }}</option>
          </select>
          <button class="primary" :disabled="running" @click="runExport">
            {{ running ? "导出中…" : "导出" }}
          </button>
        </div>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="manifest" class="pane done">
      <div class="section"><span class="t">导出完成</span></div>
      <p class="muted small">
        写入 <span class="mono">{{ outputDir }}</span> · 内容指纹
        <span class="mono">{{ manifest.content_hash.slice(0, 12) }}</span>
      </p>
      <TransitionGroup name="list" tag="div" class="files">
        <div v-for="f in manifest.files" :key="f.path" class="file">
          <b>{{ f.path }}</b>
          <span class="muted">{{ f.kind }}</span>
          <span class="mono sha">{{ f.sha256.slice(0, 10) }}</span>
        </div>
      </TransitionGroup>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 0.8rem;
}

.block {
  padding: 0.9rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.small {
  font-size: 0.8rem;
}

.row {
  display: flex;
  gap: 0.5rem;
  margin-top: auto;
}

select {
  flex: 1;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.85rem;
}

button,
a.link {
  border-radius: 0.5rem;
  cursor: pointer;
  font: inherit;
  font-size: 0.85rem;
  padding: 0.45rem 0.9rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  text-decoration: none;
  text-align: center;
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

.error {
  color: #e89a9a;
  margin-top: 0.8rem;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--ow-cyan);
}

.files {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.file {
  display: flex;
  gap: 0.7rem;
  align-items: baseline;
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.45rem 0.7rem;
  font-size: 0.84rem;
}

.file b {
  color: var(--ow-gold-bright);
}

.sha {
  margin-left: auto;
}

.list-enter-active {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}
</style>
