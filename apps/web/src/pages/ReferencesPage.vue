<script setup lang="ts">
import { onMounted, ref, watch } from "vue";
import { ApiError, apiDelete, apiGet, apiPost, currentProject, humanizeError } from "../api";
import PageHead from "../components/PageHead.vue";
import FilePicker from "../components/FilePicker.vue";
import AsyncState from "../components/AsyncState.vue";
import { notifyError } from "../toast";
import { PROVENANCE_OPTIONS, type Provenance } from "../provenance";
import { askConfirm } from "../confirm";

// Structured files go through the base64 upload branch so the server can preserve page/cell
// locators and an immutable artifact revision. EPUB remains an explicitly-labelled flat fallback.
// Everything else (.txt/.md/.json) stays the plain-text door.
const BINARY_EXTS = [".docx", ".pdf", ".xlsx", ".epub"];

interface Source {
  id?: string;
  title: string;
  source_type?: string;
  original_filename?: string;
  language?: string;
  char_count?: number;
  chunk_count?: number;
  provenance?: Provenance;
  allowed_uses?: string[];
  metadata?: Record<string, string>;
  quarantine_status?: "clear" | "quarantined" | "approved";
  quarantine_reason?: string;
  quarantine_approved_at?: string | null;
  [k: string]: unknown;
}

interface ReferenceAdded {
  source: Source;
  injection_flagged_chunks: string[];
  ingestion_mode?: "structured_locator" | "legacy_text" | "flattened_binary";
  degraded?: boolean;
  degradation_reasons?: string[];
}

interface Hit {
  ref: string;
  title: string;
  body: string;
}

const sources = ref<Source[]>([]);
const sourceTotal = ref(0);
const nextSourceCursor = ref<string | null>(null);
const flash = ref("");
const flashWarning = ref(false);
const sourcesLoading = ref(true);
const sourcesLoadingMore = ref(false);
const sourcesError = ref("");
const sourcesMoreError = ref("");
let sourcesLoadEpoch = 0;

const newTitle = ref("");
const newText = ref("");
const newFile = ref<File | null>(null);
// 素材库三种归属都收（它与正典物理隔离），默认取最保守的一档。
const newProvenance = ref<Provenance>("third_party_reference");
const newAllowDrafting = ref(false);
const adding = ref(false);
const removingId = ref<string | null>(null);
const approvingId = ref<string | null>(null);

function provenanceLabel(value: Provenance | undefined): string {
  return PROVENANCE_OPTIONS.find((opt) => opt.value === value)?.label ?? "";
}

const query = ref("");
const searching = ref(false);
const hits = ref<Hit[]>([]);
const searched = ref(false);
const searchError = ref("");
const searchDegraded = ref(false);
const searchDegradationReasons = ref<string[]>([]);

function isQuarantined(source: Source): boolean {
  return (
    source.quarantine_status === "quarantined" ||
    (source.metadata?.injection_flagged === "true" && source.quarantine_status !== "approved")
  );
}

function requestedUses(source: Source): string[] {
  const raw = source.metadata?.quarantine_requested_uses?.split(",") ?? [];
  const valid = raw.filter((use) => use === "inspiration" || use === "drafting");
  return valid.length ? [...new Set(valid)] : ["inspiration"];
}

function degradationLabel(reason: string): string {
  if (reason === "sentence_transformers_not_installed") {
    return "未安装 sentence-transformers，当前使用哈希向量基线";
  }
  if (reason.startsWith("semantic_model_load_failed:")) {
    return `语义模型加载失败（${reason.split(":")[1]}）`;
  }
  return reason;
}

function selectedAllowedUses(): string[] {
  return newAllowDrafting.value ? ["inspiration", "drafting"] : ["inspiration"];
}

async function refresh(
  cursor: string | null = null,
  append = false,
  epoch = ++sourcesLoadEpoch,
): Promise<void> {
  const params = new URLSearchParams({ limit: "30" });
  if (cursor) params.set("cursor", cursor);
  const body = await apiGet<{
    sources: Source[];
    total?: number;
    next_cursor?: string | null;
  }>(`/projects/${currentProject()}/references?${params.toString()}`);
  if (epoch !== sourcesLoadEpoch) return;
  const merged = append ? [...sources.value, ...body.sources] : body.sources;
  sources.value = merged;
  sourceTotal.value = body.total ?? merged.length;
  nextSourceCursor.value = body.next_cursor ?? null;
  if (!append) sourcesMoreError.value = "";
}

async function loadSources(): Promise<void> {
  const epoch = ++sourcesLoadEpoch;
  sourcesLoading.value = true;
  sourcesError.value = "";
  sourcesMoreError.value = "";
  try {
    await refresh(null, false, epoch);
  } catch (e) {
    sourcesError.value = humanizeError(e);
  } finally {
    sourcesLoading.value = false;
  }
}

async function loadMoreSources(): Promise<void> {
  const cursor = nextSourceCursor.value;
  if (!cursor || sourcesLoadingMore.value) return;
  const epoch = sourcesLoadEpoch;
  sourcesLoadingMore.value = true;
  sourcesMoreError.value = "";
  try {
    await refresh(cursor, true, epoch);
  } catch (e) {
    if (epoch === sourcesLoadEpoch && e instanceof ApiError && [409, 422].includes(e.status)) {
      sourcesLoadingMore.value = false;
      await loadSources();
      return;
    }
    if (epoch === sourcesLoadEpoch) sourcesMoreError.value = humanizeError(e);
  } finally {
    if (epoch === sourcesLoadEpoch) sourcesLoadingMore.value = false;
  }
}

onMounted(loadSources);

watch(query, () => {
  hits.value = [];
  searched.value = false;
  searchError.value = "";
  searchDegraded.value = false;
  searchDegradationReasons.value = [];
});

function onFile(file: File): void {
  if (!newTitle.value.trim()) newTitle.value = file.name.replace(/\.[^.]+$/, "");
  const ext = file.name.slice(file.name.lastIndexOf(".")).toLowerCase();
  if (BINARY_EXTS.includes(ext)) {
    newFile.value = file;
    newText.value = "";
    return;
  }
  newFile.value = null;
  const reader = new FileReader();
  reader.onload = () => {
    newText.value = String(reader.result ?? "");
  };
  reader.readAsText(file);
}

async function fileToBase64(file: File): Promise<string> {
  const buffer = await file.arrayBuffer();
  const bytes = new Uint8Array(buffer);
  let binary = "";
  const chunk = 0x8000; // avoid a stack-overflowing single String.fromCharCode(...bytes) call
  for (let i = 0; i < bytes.length; i += chunk) {
    binary += String.fromCharCode(...bytes.subarray(i, i + chunk));
  }
  return btoa(binary);
}

async function add(): Promise<void> {
  const title = newTitle.value.trim();
  if (!title || (!newText.value.trim() && !newFile.value) || adding.value) return;
  adding.value = true;
  flash.value = "";
  flashWarning.value = false;
  try {
    let created: ReferenceAdded;
    if (newFile.value) {
      created = await apiPost<ReferenceAdded>(`/projects/${currentProject()}/references:upload`, {
        title,
        filename: newFile.value.name,
        content_base64: await fileToBase64(newFile.value),
        provenance: newProvenance.value,
        allowed_uses: selectedAllowedUses(),
      });
    } else {
      created = await apiPost<ReferenceAdded>(`/projects/${currentProject()}/references`, {
        title,
        text: newText.value,
        source_type: "uploaded_file",
        provenance: newProvenance.value,
        allowed_uses: selectedAllowedUses(),
      });
    }
    const flagged = created.injection_flagged_chunks.length;
    const degraded = created.degraded ?? false;
    flashWarning.value = flagged > 0 || degraded;
    const messages = [`已收录「${title}」。`];
    if (created.ingestion_mode === "structured_locator") {
      messages.push("页码/单元格定位与不可变文件版本已保留。");
    }
    if (degraded) {
      messages.push(`本次使用退化解析：${(created.degradation_reasons ?? []).join("、")}。`);
    }
    if (flagged) {
      messages.push(
        `有 ${flagged} 个片段命中间接提示注入规则，已自动隔离且未进入检索索引。请在素材列表复核后手动批准。`,
      );
    }
    flash.value = messages.join("");
    newTitle.value = "";
    newText.value = "";
    newFile.value = null;
    newAllowDrafting.value = false;
    await refresh();
  } catch (e) {
    notifyError(e);
  } finally {
    adding.value = false;
  }
}

async function approve(source: Source): Promise<void> {
  const id = source.id;
  if (!id || approvingId.value || !isQuarantined(source)) return;
  const uses = requestedUses(source);
  const useLabel = uses.includes("drafting") ? "创世/扩写及草稿" : "仅创世/扩写";
  if (
    !(await askConfirm(
      `仅在你已阅读并确认其中的指令性文本只是素材内容时批准。批准后「${source.title}」将恢复为：${useLabel}。继续吗？`,
      { title: "人工批准隔离素材", confirmLabel: "确认批准并入索引" },
    ))
  ) {
    return;
  }
  approvingId.value = id;
  flash.value = "";
  flashWarning.value = false;
  try {
    await apiPost(`/projects/${currentProject()}/references/${encodeURIComponent(id)}:approve`, {
      allowed_uses: uses,
      note: "approved_in_web_reference_review",
    });
    flash.value = `已人工批准「${source.title}」，素材已按“${useLabel}”进入检索索引。`;
    await refresh();
  } catch (e) {
    notifyError(e);
  } finally {
    approvingId.value = null;
  }
}

async function remove(source: Source): Promise<void> {
  const id = source.id;
  if (!id || removingId.value) return;
  const label = source.title || id;
  if (
    !(await askConfirm(`确定删除素材「${label}」？此操作不可撤销，已入档内容对它的引用会失效。`, {
      title: "删除参考素材",
      confirmLabel: "删除素材",
    }))
  ) {
    return;
  }
  removingId.value = id;
  flash.value = "";
  try {
    const body = await apiDelete<{ dangling_ref_warnings: string[] }>(
      `/projects/${currentProject()}/references/${encodeURIComponent(id)}`,
    );
    flash.value = body.dangling_ref_warnings.length
      ? `已删除「${label}」。以下已入档内容引用了它，现已失效：${body.dangling_ref_warnings.join("、")}`
      : `已删除「${label}」。`;
    await refresh();
  } catch (e) {
    notifyError(e);
  } finally {
    removingId.value = null;
  }
}

async function search(): Promise<void> {
  if (!query.value.trim() || searching.value) return;
  const requestedQuery = query.value.trim();
  searching.value = true;
  searched.value = false;
  searchError.value = "";
  searchDegraded.value = false;
  searchDegradationReasons.value = [];
  hits.value = [];
  try {
    const body = await apiPost<{
      hits: Hit[];
      degraded?: boolean;
      degradation_reasons?: string[];
    }>(`/projects/${currentProject()}/references:search`, { query: requestedQuery });
    if (query.value.trim() === requestedQuery) {
      hits.value = body.hits;
      searchDegraded.value = body.degraded ?? false;
      searchDegradationReasons.value = body.degradation_reasons ?? [];
      searched.value = true;
    }
  } catch (e) {
    if (query.value.trim() === requestedQuery) {
      searchError.value = humanizeError(e);
      searched.value = true;
    }
  } finally {
    searching.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead overline="REFERENCES" title="灵感库" purpose="收录参考素材；用途由你逐份授权，素材本身不入正式设定。" />

    <div class="grid">
      <div class="pane block">
        <div class="section"><span class="t">收录素材</span></div>
        <input v-model="newTitle" maxlength="200" placeholder="标题" aria-label="素材标题" />
        <label class="pick">
          <span class="muted small">来源归属</span>
          <select v-model="newProvenance">
            <option v-for="opt in PROVENANCE_OPTIONS" :key="opt.value" :value="opt.value">
              {{ opt.label }} — {{ opt.hint }}
            </option>
          </select>
        </label>
        <label class="grant-option">
          <input v-model="newAllowDrafting" type="checkbox" />
          <span>
            <b>允许任务、人设与对白草稿借鉴</b>
            <small>开启后会加入 drafting 用途；生成结果保留来源并执行泄漏检查，仍需人审。</small>
          </span>
        </label>
        <FilePicker
          accept=".txt,.md,.json,.docx,.pdf,.xlsx,.epub"
          hint="选择 .txt / .md / .json / .docx / .pdf / .xlsx / .epub，或拖入"
          @select="onFile"
        />
        <p v-if="newFile" class="muted small">
          已选择「{{ newFile.name }}」，将作为文件整篇上传解析（不占用下方文本框）。
        </p>
        <textarea v-model="newText" rows="5" placeholder="粘贴文本，或选一个 .txt/.md/.json 文件自动读入" aria-label="素材正文"></textarea>
        <button
          class="primary"
          :disabled="adding || !newTitle.trim() || (!newText.trim() && !newFile)"
          @click="add"
        >
          {{ adding ? "收录中…" : "收录" }}
        </button>
        <p v-if="flash" class="flash" :class="{ warning: flashWarning }">{{ flash }}</p>
      </div>

      <div class="pane block">
        <div class="section"><span class="t">灵感检索</span></div>
        <div class="row">
          <input v-model="query" placeholder="按主题/意象检索素材" aria-label="素材检索词" @keydown.enter="search" />
          <button class="ghost" :disabled="searching || !query.trim()" @click="search">
            {{ searching ? "检索中…" : "检索" }}
          </button>
        </div>
        <AsyncState
          v-if="searching"
          state="loading"
          title="正在检索灵感库"
          :hint="`查找与「${query.trim()}」相关的素材片段。`"
          compact
        />
        <AsyncState
          v-else-if="searchError"
          state="error"
          title="检索失败"
          :hint="searchError"
          action-label="重新检索"
          compact
          @action="search"
        />
        <div v-if="searchDegraded" class="degradation" role="status">
          <b>本次素材检索使用了回退路径。</b>
          <span v-for="reason in searchDegradationReasons" :key="reason">
            {{ degradationLabel(reason) }}
          </span>
        </div>
        <div v-if="hits.length" class="hits">
          <div v-for="h in hits" :key="h.ref" class="hit">
            <span class="mono">{{ h.ref }}</span>
            <b>{{ h.title }}</b>
            <span class="muted body">{{ h.body }}</span>
          </div>
        </div>
        <AsyncState
          v-else-if="searched"
          state="empty"
          title="没有匹配的素材"
          hint="换一个更宽泛的主题、意象或关键词再试。"
          compact
        />
        <p v-else-if="query" class="muted small">敲回车或点检索。</p>
      </div>
    </div>

    <div class="section"><span class="t">全部素材</span></div>
    <AsyncState
      v-if="sourcesLoading"
      state="loading"
      title="正在读取灵感库"
      hint="已收录的参考素材会显示在这里。"
      compact
    />
    <AsyncState
      v-else-if="sourcesError"
      state="error"
      title="灵感库读取失败"
      :hint="sourcesError"
      action-label="重新读取"
      compact
      @action="loadSources"
    />
    <AsyncState
      v-else-if="!sources.length"
      state="empty"
      title="还没有素材"
      hint="在上方收录第一份参考资料；它只用于检索，不会直接写入正式设定。"
      compact
    />
    <TransitionGroup v-else name="list" tag="div" class="sources">
      <div
        v-for="(s, i) in sources"
        :key="s.id ?? i"
        class="pane src"
        :class="{ unsafe: isQuarantined(s) }"
      >
        <div class="src-main">
          <b>{{ s.title }}</b>
          <span class="muted meta">{{ s.source_type ?? "素材" }}<template v-if="provenanceLabel(s.provenance)"> · {{ provenanceLabel(s.provenance) }}</template><template v-if="s.allowed_uses?.includes('drafting')"> · 可用于草稿</template><template v-else> · 仅创世/扩写</template><template v-if="s.language"> · {{ s.language }}</template><template v-if="s.char_count"> · {{ s.char_count.toLocaleString() }} 字</template><template v-if="s.chunk_count"> · {{ s.chunk_count }} 块可检索</template></span>
          <strong v-if="isQuarantined(s)" class="injection-warning">
            已隔离：命中间接提示注入规则，当前未进入任何检索或生成链路。请先阅读原始素材，再显式批准。
          </strong>
          <strong v-else-if="s.quarantine_status === 'approved'" class="approval-note">
            已人工批准隔离素材<template v-if="s.quarantine_approved_at"> · {{ new Date(s.quarantine_approved_at).toLocaleString() }}</template>
          </strong>
        </div>
        <div class="source-actions">
          <button
            v-if="isQuarantined(s)"
            class="ghost approve"
            :disabled="approvingId === s.id"
            @click="approve(s)"
          >
            {{ approvingId === s.id ? "批准中…" : "人工复核并批准" }}
          </button>
          <button
            class="ghost danger"
            :disabled="removingId === s.id"
            @click="remove(s)"
          >
            {{ removingId === s.id ? "删除中…" : "删除" }}
          </button>
        </div>
      </div>
    </TransitionGroup>
    <div v-if="sources.length" class="pager" aria-label="灵感库分页">
      <span class="muted small">已显示 {{ sources.length }} / {{ sourceTotal }} 份素材</span>
      <button
        v-if="nextSourceCursor"
        class="ghost"
        :disabled="sourcesLoadingMore"
        @click="loadMoreSources"
      >
        {{ sourcesLoadingMore ? "加载中…" : "加载下一页" }}
      </button>
      <span v-else class="muted small">已到末页</span>
      <span v-if="sourcesMoreError" class="error small" role="alert">{{ sourcesMoreError }}</span>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 300px), 1fr));
  gap: 0.8rem;
  margin-bottom: 1rem;
}

.block {
  padding: 0.9rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.row {
  display: flex;
  gap: 0.5rem;
}

input,
textarea,
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

.row input {
  flex: 1;
}

.pick {
  display: flex;
  align-items: center;
  gap: 0.5rem;
}

.pick select {
  flex: 1;
  min-width: 0;
}

.grant-option {
  display: flex;
  align-items: flex-start;
  gap: 0.55rem;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-muted);
  font-size: 0.8rem;
}

.grant-option input {
  flex: none;
  width: 1rem;
  height: 1rem;
  margin-top: 0.15rem;
  padding: 0;
  accent-color: var(--ow-gold);
}

.grant-option span,
.grant-option small {
  display: block;
}

.grant-option b {
  color: var(--ow-ink);
}

.grant-option small {
  margin-top: 0.2rem;
  line-height: 1.45;
}

textarea {
  resize: vertical;
}

input:focus,
textarea:focus,
select:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

button {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
  font-size: 0.85rem;
  padding: 0.45rem 0.9rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
  align-self: flex-start;
}

button:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.flash {
  color: #8ed4ac;
  font-size: 0.82rem;
}

.flash.warning {
  color: #e89a9a;
  font-weight: 600;
}

.error {
  color: #e89a9a;
}

.small {
  font-size: 0.78rem;
}

.hits {
  display: flex;
  flex-direction: column;
  gap: 0.4rem;
  margin-top: 0.5rem;
}

.hit {
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-panel-2);
  padding: 0.45rem 0.65rem;
  font-size: 0.83rem;
}

.hit b {
  color: var(--ow-gold-bright);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.72rem;
  color: var(--ow-cyan);
}

.body {
  font-size: 0.8rem;
}

.sources {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.src {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
  padding: 0.55rem 0.9rem;
}

.src-main {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.7rem;
  min-width: 0;
}

.src b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

button.ghost.danger {
  flex: none;
  font-size: 0.78rem;
  padding: 0.32rem 0.7rem;
  color: var(--ow-muted);
}

button.ghost.danger:hover:not(:disabled) {
  border-color: #e08585;
  color: #e08585;
}

.meta {
  font-size: 0.78rem;
}

.list-enter-active,
.list-move {
  transition: all 0.3s ease;
}

.list-enter-from {
  opacity: 0;
  transform: translateY(6px);
}

.source-actions {
  display: flex;
  flex: none;
  gap: 0.4rem;
}

button.ghost.approve {
  color: var(--ow-gold-bright);
}

.degradation {
  display: grid;
  gap: 0.15rem;
  margin-top: 0.5rem;
  padding: 0.5rem 0.65rem;
  border-left: 2px solid #e6c07e;
  background: rgba(230, 192, 126, 0.08);
  color: var(--ow-muted);
  font-size: 0.76rem;
}

.degradation b {
  color: var(--ow-ink);
}

.src.unsafe {
  border-color: rgba(224, 133, 133, 0.7);
}

.injection-warning {
  flex-basis: 100%;
  color: #e89a9a;
  font-size: 0.76rem;
  font-weight: 600;
}

.approval-note {
  flex-basis: 100%;
  color: #8ed4ac;
  font-size: 0.76rem;
}

.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.7rem;
  margin-top: 0.7rem;
}

@media (max-width: 640px) {
  .block {
    padding: 0.8rem;
  }

  .row,
  .pick {
    align-items: stretch;
    flex-direction: column;
  }

  .row input,
  .row button,
  .pick select,
  button.primary {
    width: 100%;
    min-height: 44px;
  }

  .src {
    align-items: stretch;
    flex-direction: column;
  }

  .src-main {
    align-items: flex-start;
    flex-direction: column;
    gap: 0.3rem;
  }

  .src button {
    min-height: 44px;
    width: 100%;
  }

  .source-actions {
    width: 100%;
    flex-direction: column;
  }

  .pager {
    align-items: stretch;
    flex-direction: column;
  }

  .pager button {
    min-height: 44px;
    width: 100%;
  }
}
</style>
