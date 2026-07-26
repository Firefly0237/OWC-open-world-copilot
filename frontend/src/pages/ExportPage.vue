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

// data-pack export options
const targetEngine = ref("generic");
const unityLocalization = ref(false);
const rowStructMap = ref<Record<string, Record<string, string>> | null>(null);
const rowStructMapName = ref("");
const rowStructMapError = ref("");

// Parse + validate the Unreal column-rename map ({table: {canonical: exportName}}) in the browser,
// so a malformed file is caught with a plain-language message before we ever call the export.
async function onRowStructMapFile(e: Event): Promise<void> {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  rowStructMapError.value = "";
  rowStructMap.value = null;
  rowStructMapName.value = "";
  if (!file) return;
  let data: unknown;
  try {
    data = JSON.parse(await file.text());
  } catch {
    rowStructMapError.value = "这个文件不是合法的 JSON，请检查后重新选择。";
    return;
  }
  if (typeof data !== "object" || data === null || Array.isArray(data)) {
    rowStructMapError.value = '映射必须是 JSON 对象，形如 {"表名": {"原字段": "导出名"}}。';
    return;
  }
  for (const [table, fields] of Object.entries(data)) {
    const ok =
      typeof fields === "object" &&
      fields !== null &&
      !Array.isArray(fields) &&
      Object.values(fields).every((v) => typeof v === "string");
    if (!ok) {
      rowStructMapError.value = `表「${table}」的值必须是「原字段 → 导出名」的对象。`;
      return;
    }
  }
  rowStructMap.value = data as Record<string, Record<string, string>>;
  rowStructMapName.value = file.name;
}

const importText = ref("");
const importing = ref(false);
const importError = ref("");
const importPlan = ref<ImportPlan | null>(null);
const showRoundtrip = ref(false);
const showHash = ref(false);

async function runExport(): Promise<void> {
  running.value = true;
  error.value = "";
  manifest.value = null;
  try {
    const payload: Record<string, unknown> = { target_engine: targetEngine.value };
    if (targetEngine.value === "unity") payload.unity_localization = unityLocalization.value;
    if (targetEngine.value === "unreal" && rowStructMap.value)
      payload.row_struct_map = rowStructMap.value;
    const body = await apiPost<{ output_dir: string; manifest: Manifest }>(
      `/projects/${project}/exports`,
      payload,
    );
    outputDir.value = body.output_dir;
    manifest.value = body.manifest;
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    running.value = false;
  }
}

function copyPath(): void {
  void navigator.clipboard.writeText(outputDir.value);
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
          将世界数据导出为标准格式，包含给程序员用的数据文件和给本地化团队用的翻译文件。
        </p>
        <label class="cfg-row">
          <span class="cfg-label">目标引擎</span>
          <select v-model="targetEngine">
            <option value="generic">通用（引擎无关）</option>
            <option value="unreal">Unreal</option>
            <option value="unity">Unity</option>
          </select>
        </label>
        <label v-if="targetEngine === 'unity'" class="cfg-check">
          <input v-model="unityLocalization" type="checkbox" />
          <span>附带 Unity 本地化字符串表（String Table CSV）</span>
        </label>
        <div v-if="targetEngine === 'unreal'" class="cfg-file">
          <span class="cfg-label">字段映射（可选）</span>
          <p class="muted tiny">
            把导出数据表的列名换成你 Unreal 工程里的字段名。上传 JSON，形如
            <code class="mono">{"表名":{"原字段":"导出名"}}</code>；不上传则用默认列名。
          </p>
          <input type="file" accept="application/json,.json" @change="onRowStructMapFile" />
          <p v-if="rowStructMapName" class="ok-file tiny">已载入映射：{{ rowStructMapName }}</p>
          <p v-if="rowStructMapError" class="error tiny">{{ rowStructMapError }}</p>
        </div>
        <div class="row">
          <button class="primary" :disabled="running" @click="runExport">
            {{ running ? "导出中…" : "导出数据包" }}
          </button>
        </div>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div class="pane block roundtrip">
      <button class="roundtrip-toggle" @click="showRoundtrip = !showRoundtrip">
        <span class="roundtrip-label">从引擎回拉（需要程序员配合）</span>
        <span class="roundtrip-caret" :class="{ open: showRoundtrip }">▾</span>
      </button>
      <template v-if="showRoundtrip">
        <p class="muted small">
          这个功能用于游戏引擎修改了任务内容后，把改动同步回来。通常需要程序员从引擎导出数据。如果不确定，可以跳过。
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
      </template>
    </div>

    <RecognizePanel />

    <div v-if="manifest" class="pane done">
      <div class="section"><span class="t">导出完成</span></div>
      <div class="export-path-block">
        <span class="export-path-label">文件已写入本机路径：</span>
        <code class="export-path mono">{{ outputDir }}</code>
        <button class="copy-btn" title="复制路径" @click="copyPath">复制路径</button>
      </div>
      <p class="muted small export-hint">
        ⓘ 文件保存在服务器本机，不支持直接浏览器下载。在资源管理器中粘贴上方路径即可访问，或将整个文件夹拷贝给程序员 / 本地化团队。
        <button class="hash-toggle" @click="showHash = !showHash">
          文件校验码 <span :class="{ open: showHash }">▾</span>
        </button>
      </p>
      <p v-if="showHash" class="muted small hash-detail">
        sha256 指纹：<span class="mono">{{ manifest.content_hash }}</span>
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

.tiny {
  font-size: 0.74rem;
  line-height: 1.5;
}

.cfg-row {
  display: flex;
  align-items: center;
  gap: 0.6rem;
}

.cfg-label {
  font-size: 0.8rem;
  color: var(--ow-muted);
  white-space: nowrap;
}

.cfg-check {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  font-size: 0.82rem;
}

.cfg-file {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  padding: 0.5rem 0.6rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: rgba(16, 22, 48, 0.4);
}

.cfg-file input[type="file"] {
  font-size: 0.78rem;
  color: var(--ow-muted);
}

.cfg-file code {
  color: var(--ow-cyan);
  word-break: break-all;
}

.ok-file {
  color: #8ed4ac;
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
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.45rem 0.6rem;
  font: inherit;
  font-size: 0.85rem;
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

.roundtrip-toggle {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  width: 100%;
  background: transparent;
  border: none;
  border-radius: 0;
  color: var(--ow-ink);
  padding: 0;
  cursor: pointer;
  font: inherit;
  font-size: 0.9rem;
  text-align: left;
}

.roundtrip-label {
  font-weight: 600;
  color: var(--ow-gold-bright);
}

.roundtrip-caret {
  margin-left: auto;
  color: var(--ow-muted);
  transition: transform 0.2s ease;
}

.roundtrip-caret.open {
  transform: rotate(180deg);
}

.hash-toggle {
  background: transparent;
  border: none;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.78rem;
  cursor: pointer;
  padding: 0 0.3rem;
  text-decoration: underline dotted;
}

.hash-detail {
  margin-top: 0.2rem;
  word-break: break-all;
}

.export-path-block {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin-bottom: 0.45rem;
  padding: 0.55rem 0.8rem;
  background: rgba(16, 22, 48, 0.6);
  border: 1px solid var(--ow-gold-soft);
  border-radius: var(--ow-control-radius);
}

.export-path-label {
  font-size: 0.8rem;
  color: var(--ow-gold-bright);
  white-space: nowrap;
}

.export-path {
  flex: 1;
  min-width: 0;
  word-break: break-all;
  font-size: 0.82rem;
}

.copy-btn {
  background: var(--ow-gold-faint);
  border: 1px solid var(--ow-gold-soft);
  border-radius: var(--ow-control-radius);
  color: var(--ow-gold-bright);
  font: inherit;
  font-size: 0.78rem;
  padding: 0.25rem 0.65rem;
  cursor: pointer;
  white-space: nowrap;
  flex: none;
}

.copy-btn:hover {
  box-shadow: 0 0 8px rgba(240, 210, 138, 0.25);
}

.export-hint {
  margin-bottom: 0.4rem;
  line-height: 1.6;
}

textarea.import {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
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
  border-radius: var(--ow-control-radius);
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
