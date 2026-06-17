<script setup lang="ts">
import { ref } from "vue";
import { humanizeError, apiPost, apiUrl, currentProject } from "../api";
import PageHead from "../components/PageHead.vue";
import RecognizePanel from "../components/RecognizePanel.vue";

interface Manifest {
  target_engine: string;
  content_hash: string;
  files: { path: string; kind: string; sha256: string }[];
}

interface ImportPlan {
  new: string[];
  changed: string[];
  unchanged: string[];
  review_item_id: string | null;
}

const running = ref(false);
const error = ref("");
const outputDir = ref("");
const manifest = ref<Manifest | null>(null);
const project = currentProject();

const importText = ref("");
const importing = ref(false);
const importError = ref("");
const importPlan = ref<ImportPlan | null>(null);

async function runExport(): Promise<void> {
  running.value = true;
  error.value = "";
  manifest.value = null;
  try {
    const body = await apiPost<{ output_dir: string; manifest: Manifest }>(
      `/projects/${project}/exports`,
      { target_engine: "generic" },
    );
    outputDir.value = body.output_dir;
    manifest.value = body.manifest;
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    running.value = false;
  }
}

async function runImport(): Promise<void> {
  importing.value = true;
  importError.value = "";
  importPlan.value = null;
  let quests: unknown;
  try {
    quests = JSON.parse(importText.value);
  } catch {
    importError.value = "粘贴的内容不是合法 JSON，请检查从引擎导出的任务行数组。";
    importing.value = false;
    return;
  }
  if (!Array.isArray(quests) || quests.length === 0) {
    importError.value = "需要一个非空的任务行数组（[]）。";
    importing.value = false;
    return;
  }
  try {
    importPlan.value = await apiPost<ImportPlan>(`/projects/${project}/engine:import`, { quests });
  } catch (e) {
    importError.value = humanizeError(e);
  } finally {
    importing.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="EXPORT" title="导出交付" purpose="出设定集、数据包（含本地化）或整库备份。" />

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
        <div class="section"><span class="t">数据包导出</span></div>
        <p class="muted small">
          结构化 JSON（content_bundle）+ 本地化 XLIFF / CSV + sha256 校验清单，写入项目目录，供任意引擎管线读取。
        </p>
        <div class="row">
          <button class="primary" :disabled="running" @click="runExport">
            {{ running ? "导出中…" : "导出数据包" }}
          </button>
        </div>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="pane block roundtrip">
      <div class="section"><span class="t">从引擎回拉</span></div>
      <p class="muted small">
        把引擎侧改过的任务行（JSON 数组）粘进来，系统按内容指纹比对正典，新增/改动的任务进审阅队列等人审，
        正典不会被直接覆盖。
      </p>
      <textarea
        v-model="importText"
        class="import"
        rows="5"
        placeholder='[{"id": "quest_x", "title": "…", "objective": "引擎侧改过的目标"}]'
      ></textarea>
      <div class="row">
        <button class="primary" :disabled="importing || !importText.trim()" @click="runImport">
          {{ importing ? "比对中…" : "回拉并送审" }}
        </button>
      </div>
      <p v-if="importError" class="error">{{ importError }}</p>
      <div v-if="importPlan" class="plan">
        <span class="tag new">新增 {{ importPlan.new.length }}</span>
        <span class="tag changed">改动 {{ importPlan.changed.length }}</span>
        <span class="tag unchanged">未变 {{ importPlan.unchanged.length }}</span>
        <span v-if="importPlan.review_item_id" class="muted small">已送审阅台，去「审阅」页处理。</span>
        <span v-else class="muted small">没有需要送审的变更。</span>
      </div>
    </div>

    <RecognizePanel />

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

.roundtrip {
  margin-top: 0.9rem;
}

textarea.import {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.55rem 0.7rem;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.8rem;
  resize: vertical;
}

.plan {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
  margin-top: 0.6rem;
}

.tag {
  border-radius: 0.5rem;
  padding: 0.2rem 0.55rem;
  font-size: 0.78rem;
  border: 1px solid var(--ow-line);
}

.tag.new {
  color: var(--ow-gold-bright);
  border-color: rgba(240, 210, 138, 0.5);
}

.tag.changed {
  color: var(--ow-cyan);
  border-color: rgba(120, 200, 220, 0.5);
}

.tag.unchanged {
  color: var(--ow-ink-dim);
}
</style>
