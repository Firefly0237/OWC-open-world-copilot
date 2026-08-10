<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiDownload, apiGet, humanizeError, apiPost, currentProject } from "../api";
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
  conflicts: string[];
  library_ahead: string[];
  engine_missing: string[];
  baseline_provided: boolean;
  warnings: string[];
  review_item_id: string | null;
}

interface ProseMention {
  name: string;
  ref: string;
  count: number;
}

interface ProseIssue {
  kind: "forbidden_term" | "unknown_mention" | string;
  excerpt: string;
  message: string;
  position: number;
  suggestion: string;
}

interface ProseCheckResult {
  request_id: string;
  project: string;
  resolved_mentions: ProseMention[];
  issues: ProseIssue[];
  stats: Record<string, number>;
  cost_budget: Record<string, unknown>;
}

const running = ref(false);
const error = ref("");
const outputDir = ref("");
const manifest = ref<Manifest | null>(null);
const project = currentProject();
const downloading = ref("");
const managedWorkspace = ref<boolean | null>(null);

onMounted(async () => {
  try {
    const query = new URLSearchParams({ limit: "1", focus: project });
    const body = await apiGet<{ workspaces: { name: string }[] }>(
      `/workspaces?${query.toString()}`,
    );
    managedWorkspace.value = body.workspaces.some((workspace) => workspace.name === project);
  } catch {
    // Exporting the normal lore/data surfaces remains available even if this optional probe fails.
    managedWorkspace.value = true;
  }
});

// data-pack export options
const targetEngine = ref("generic");
const unityLocalization = ref(false);
const planningWorkbook = ref(false);
const planningNotes = ref(false);
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
const baselineText = ref("");
const importing = ref(false);
const importError = ref("");
const importPlan = ref<ImportPlan | null>(null);
const showRoundtrip = ref(false);
const showHash = ref(false);
const proseText = ref("");
const proseChecking = ref(false);
const proseError = ref("");
const proseResult = ref<ProseCheckResult | null>(null);

function proseIssueLabel(kind: string): string {
  if (kind === "forbidden_term") return "禁用写法";
  if (kind === "unknown_mention") return "未知专名";
  return kind;
}

function invalidateProseResult(): void {
  proseResult.value = null;
  proseError.value = "";
}

async function runProseCheck(): Promise<void> {
  if (!proseText.value.trim() || proseChecking.value) return;
  proseChecking.value = true;
  proseError.value = "";
  proseResult.value = null;
  try {
    proseResult.value = await apiPost<ProseCheckResult>(
      `/projects/${encodeURIComponent(project)}/prose:check`,
      { text: proseText.value },
    );
  } catch (caught) {
    proseError.value = humanizeError(caught);
  } finally {
    proseChecking.value = false;
  }
}

async function runExport(): Promise<void> {
  running.value = true;
  error.value = "";
  manifest.value = null;
  try {
    const payload: Record<string, unknown> = {
      target_engine: targetEngine.value,
      planning_workbook: planningWorkbook.value,
      planning_notes: planningNotes.value,
    };
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

async function download(path: string, filename: string, key: string): Promise<void> {
  if (downloading.value) return;
  downloading.value = key;
  error.value = "";
  try {
    await apiDownload(path, filename);
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    downloading.value = "";
  }
}

async function runImport(): Promise<void> {
  importing.value = true;
  importError.value = "";
  importPlan.value = null;
  let quests: unknown;
  let baselineQuests: unknown;
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
    baselineQuests = JSON.parse(baselineText.value);
  } catch {
    importError.value = "导出基线不是合法 JSON；请粘贴当初交给引擎的任务行数组。";
    importing.value = false;
    return;
  }
  if (!Array.isArray(baselineQuests) || baselineQuests.length === 0) {
    importError.value = "为避免覆盖资料库里的新改动，回拉必须提供非空的原始导出基线。";
    importing.value = false;
    return;
  }
  try {
    importPlan.value = await apiPost<ImportPlan>(`/projects/${project}/engine:import`, {
      quests,
      baseline_quests: baselineQuests,
    });
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
          <button class="ghost link" :disabled="Boolean(downloading)" @click="download(`/projects/${project}/lorebook?fmt=md`, `${project}-lorebook.md`, 'md')">
            {{ downloading === "md" ? "准备中…" : "下载 .md" }}
          </button>
          <button class="ghost link" :disabled="Boolean(downloading)" @click="download(`/projects/${project}/lorebook?fmt=docx`, `${project}-lorebook.docx`, 'docx')">
            {{ downloading === "docx" ? "准备中…" : "下载 .docx" }}
          </button>
        </div>
      </div>

      <div class="pane block" :class="{ unavailable: managedWorkspace === false }">
        <div class="section"><span class="t">世界包备份</span></div>
        <p v-if="managedWorkspace === false" class="muted small">
          当前世界由外部目录注册，不属于工作区存储；请在原目录中完成备份。设定集和数据包仍可正常导出。
        </p>
        <p v-else class="muted small">整个世界打包成 .zip，可在「工作区」页导入，用于备份或换机。</p>
        <div class="row">
          <button class="ghost link" :disabled="Boolean(downloading) || managedWorkspace !== true" @click="download(`/workspaces/${encodeURIComponent(project)}/pack`, `${project}.zip`, 'pack')">
            {{ managedWorkspace === null ? "正在确认工作区…" : downloading === "pack" ? "打包中…" : managedWorkspace ? "下载世界包" : "外部世界不支持整包" }}
          </button>
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
        <label class="cfg-check">
          <input v-model="planningWorkbook" type="checkbox" />
          <span>附带策划工作簿（任务、角色与交付清单）</span>
        </label>
        <label class="cfg-check">
          <input v-model="planningNotes" type="checkbox" />
          <span>附带策划说明文档与当前审查发现</span>
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

    <div class="pane prose-check">
      <div class="tool-head">
        <div>
          <div class="section"><span class="t">交付前文稿检查</span></div>
          <p class="muted small">
            粘贴章节或任务文案，核对世界档案中的人物、地点与术语；同时找出禁用写法和疑似未入档专名。
          </p>
        </div>
        <span class="zero-badge">确定性规则 · $0</span>
      </div>
      <textarea
        v-model="proseText"
        class="prose-input"
        rows="7"
        maxlength="200000"
        placeholder="粘贴准备交付的章节、任务对白或策划文案…"
        :disabled="proseChecking"
        @input="invalidateProseResult"
      ></textarea>
      <div class="prose-footer">
        <span class="muted tiny">{{ proseText.length.toLocaleString() }} / 200,000 字符 · 不调用模型</span>
        <div class="row prose-actions">
          <button
            class="ghost"
            :disabled="proseChecking || (!proseText && !proseResult)"
            @click="proseText = ''; invalidateProseResult()"
          >
            清空
          </button>
          <button class="primary" :disabled="proseChecking || !proseText.trim()" @click="runProseCheck">
            {{ proseChecking ? "逐条核对中…" : "开始检查" }}
          </button>
        </div>
      </div>

      <p v-if="proseError" class="error prose-error" role="alert">{{ proseError }}</p>
      <div v-if="proseChecking" class="prose-loading" aria-live="polite">
        正在对照档案解析人物、地点与术语…
      </div>

      <div v-else-if="proseResult" class="prose-result" aria-live="polite">
        <div class="result-summary">
          <span class="result-chip">正文 {{ proseResult.stats.chars ?? 0 }} 字符</span>
          <span class="result-chip">已识别 {{ proseResult.stats.resolved_mentions ?? 0 }} 项</span>
          <span class="result-chip" :class="{ danger: proseResult.issues.length }">
            待处理 {{ proseResult.stats.issues ?? proseResult.issues.length }} 项
          </span>
          <span v-if="proseResult.stats.forbidden_terms" class="result-chip danger">
            禁用写法 {{ proseResult.stats.forbidden_terms }}
          </span>
          <span v-if="proseResult.stats.unknown_mentions" class="result-chip warn">
            未知专名 {{ proseResult.stats.unknown_mentions }}
          </span>
        </div>

        <div v-if="!proseResult.issues.length" class="success-banner">
          <b>检查通过</b>
          <span>没有发现禁用写法或疑似未入档专名，可以继续交付。</span>
        </div>

        <div v-else class="prose-issues">
          <div class="section sub"><span class="t">需要处理</span></div>
          <article
            v-for="(issue, index) in proseResult.issues"
            :key="`${issue.kind}:${issue.position}:${index}`"
            class="prose-issue"
            :class="issue.kind"
          >
            <div class="issue-head">
              <span class="issue-kind">{{ proseIssueLabel(issue.kind) }}</span>
              <b>{{ issue.message }}</b>
              <span class="mono issue-position">位置 {{ issue.position + 1 }}</span>
            </div>
            <p class="excerpt">“{{ issue.excerpt }}”</p>
            <p v-if="issue.suggestion" class="suggestion">建议：{{ issue.suggestion }}</p>
          </article>
        </div>

        <details class="mentions" :open="proseResult.issues.length === 0">
          <summary>已在档案中识别的名称（{{ proseResult.resolved_mentions.length }}）</summary>
          <div v-if="proseResult.resolved_mentions.length" class="mention-grid">
            <div v-for="mention in proseResult.resolved_mentions" :key="mention.ref" class="mention">
              <b>{{ mention.name }}</b>
              <code>{{ mention.ref }}</code>
              <span>出现 {{ mention.count }} 次</span>
            </div>
          </div>
          <p v-else class="muted small">这段文稿里还没有识别到世界档案中的名称。</p>
        </details>
      </div>
    </div>

    <div class="pane block roundtrip">
      <button class="roundtrip-toggle" @click="showRoundtrip = !showRoundtrip">
        <span class="roundtrip-label">从引擎回拉（需要程序员配合）</span>
        <span class="roundtrip-caret" :class="{ open: showRoundtrip }">▾</span>
      </button>
      <template v-if="showRoundtrip">
        <p class="muted small">
          粘贴引擎当前数据和当初交给引擎的原始导出数据。系统会做三方比对；双侧冲突、库侧更新和疑似删除只提示，不会覆盖或自动入档。
        </p>
        <label class="import-label" for="engine-baseline">原始导出基线（安全比对必填）</label>
        <textarea
          id="engine-baseline"
          v-model="baselineText"
          class="import"
          rows="4"
          placeholder='[{"id": "quest_x", "title": "导出时的标题", "objective": "导出时的目标"}]'
        ></textarea>
        <label class="import-label" for="engine-current">引擎当前任务数据</label>
        <textarea
          id="engine-current"
          v-model="importText"
          class="import"
          rows="5"
          placeholder='粘贴引擎桥导出的完整 v2 任务行（不要只复制改动字段）'
        ></textarea>
        <div class="row">
          <button class="primary" :disabled="importing || !importText.trim() || !baselineText.trim()" @click="runImport">
            {{ importing ? "比对中…" : "回拉并送审" }}
          </button>
        </div>
        <p v-if="importError" class="error">{{ importError }}</p>
        <div v-if="importPlan" class="plan">
          <span class="tag new">新增 {{ importPlan.new.length }}</span>
          <span class="tag changed">改动 {{ importPlan.changed.length }}</span>
          <span class="tag unchanged">未变 {{ importPlan.unchanged.length }}</span>
          <span v-if="importPlan.conflicts.length" class="tag conflict">冲突 {{ importPlan.conflicts.length }}</span>
          <span v-if="importPlan.library_ahead.length" class="tag warn">库侧较新 {{ importPlan.library_ahead.length }}</span>
          <span v-if="importPlan.engine_missing.length" class="tag warn">引擎缺失 {{ importPlan.engine_missing.length }}</span>
          <span v-if="importPlan.review_item_id" class="muted small">已送审阅台，去「审阅」页处理。</span>
          <span v-else class="muted small">没有需要送审的变更。</span>
          <ul v-if="importPlan.warnings.length" class="import-warnings">
            <li v-for="warning in importPlan.warnings" :key="warning">{{ warning }}</li>
          </ul>
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

.prose-check {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  margin-top: 0.9rem;
  padding: 1rem 1.1rem;
}

.tool-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 1rem;
}

.tool-head p {
  margin: 0.35rem 0 0;
  max-width: 58rem;
  line-height: 1.6;
}

.zero-badge {
  flex: none;
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0,
    100% 0,
    100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%,
    0 100%,
    0 var(--ow-chip-nip)
  );
  color: var(--ow-cyan);
  font-size: 0.72rem;
  padding: 0.18rem 0.55rem;
}

.prose-input {
  width: 100%;
  min-height: 10rem;
  resize: vertical;
  background:
    linear-gradient(rgba(16, 22, 48, 0.88), rgba(16, 22, 48, 0.88)),
    repeating-linear-gradient(0deg, transparent 0 1.65rem, rgba(143, 214, 232, 0.05) 1.65rem 1.7rem);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  font: inherit;
  font-size: 0.86rem;
  line-height: 1.7;
  padding: 0.75rem 0.85rem;
}

.prose-input:focus {
  border-color: var(--ow-gold-soft);
  outline: none;
  box-shadow: 0 0 0 2px rgba(240, 210, 138, 0.08);
}

.prose-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.8rem;
}

.prose-actions {
  flex: none;
  margin-top: 0;
}

.prose-error {
  margin: 0;
}

.prose-loading {
  border: 1px dashed rgba(143, 214, 232, 0.3);
  color: var(--ow-cyan);
  font-size: 0.82rem;
  padding: 0.75rem 0.85rem;
}

.prose-result {
  display: flex;
  flex-direction: column;
  gap: 0.7rem;
  border-top: 1px solid var(--ow-line);
  padding-top: 0.8rem;
}

.result-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
}

.result-chip {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  color: var(--ow-ink-dim);
  font-size: 0.74rem;
  padding: 0.16rem 0.55rem;
}

.result-chip.warn {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.result-chip.danger {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

.success-banner {
  display: flex;
  align-items: baseline;
  gap: 0.65rem;
  border: 1px solid rgba(142, 212, 172, 0.4);
  background: rgba(142, 212, 172, 0.06);
  color: #8ed4ac;
  padding: 0.65rem 0.8rem;
}

.success-banner span {
  color: var(--ow-ink-dim);
  font-size: 0.8rem;
}

.prose-issues {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.prose-issue {
  border: 1px solid rgba(224, 180, 106, 0.34);
  background: rgba(224, 180, 106, 0.035);
  padding: 0.65rem 0.75rem;
}

.prose-issue.forbidden_term {
  border-color: rgba(224, 133, 133, 0.4);
  background: rgba(224, 133, 133, 0.04);
}

.issue-head {
  display: flex;
  align-items: baseline;
  gap: 0.55rem;
}

.issue-head b {
  color: var(--ow-ink);
  font-size: 0.84rem;
}

.issue-kind {
  flex: none;
  border: 1px solid currentColor;
  color: #e6c07e;
  font-size: 0.68rem;
  padding: 0.04rem 0.35rem;
}

.forbidden_term .issue-kind {
  color: #e89a9a;
}

.issue-position {
  margin-left: auto;
  color: var(--ow-muted);
  white-space: nowrap;
}

.excerpt,
.suggestion {
  margin: 0.4rem 0 0;
  font-size: 0.8rem;
  line-height: 1.6;
}

.excerpt {
  color: var(--ow-ink-dim);
}

.suggestion {
  color: var(--ow-cyan);
}

.mentions {
  border-top: 1px solid var(--ow-line);
  padding-top: 0.65rem;
}

.mentions summary {
  color: var(--ow-gold-bright);
  cursor: pointer;
  font-size: 0.82rem;
}

.mention-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(13rem, 1fr));
  gap: 0.4rem;
  margin-top: 0.6rem;
}

.mention {
  display: grid;
  grid-template-columns: auto 1fr auto;
  gap: 0.45rem;
  align-items: baseline;
  min-width: 0;
  border: 1px solid var(--ow-line);
  background: rgba(16, 22, 48, 0.42);
  padding: 0.38rem 0.5rem;
  font-size: 0.74rem;
}

.mention b {
  color: var(--ow-ink);
}

.mention code {
  overflow: hidden;
  color: var(--ow-cyan);
  text-overflow: ellipsis;
  white-space: nowrap;
}

.mention span {
  color: var(--ow-muted);
  white-space: nowrap;
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

.import-label {
  display: block;
  margin: 0.75rem 0 0.35rem;
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
  font-weight: 600;
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

.tag.conflict {
  color: var(--ow-danger, #ff8e8e);
  border-color: rgba(255, 110, 110, 0.55);
}

.tag.warn {
  color: var(--ow-flag, #e7aa5f);
  border-color: rgba(231, 170, 95, 0.5);
}

.import-warnings {
  flex-basis: 100%;
  margin: 0.25rem 0 0;
  padding-left: 1.25rem;
  color: var(--ow-flag, #e7aa5f);
  font-size: 0.78rem;
  line-height: 1.55;
}

@media (max-width: 600px) {
  .grid,
  .mention-grid { grid-template-columns: 1fr; }
  .mention { grid-template-columns: 1fr; }
  .mention code { white-space: normal; overflow-wrap: anywhere; }
  .prose-actions { align-items: stretch; flex-direction: column; }
  .prose-actions button { width: 100%; min-height: 44px; }
}
</style>
