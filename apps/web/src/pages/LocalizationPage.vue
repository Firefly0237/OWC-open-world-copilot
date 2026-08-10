<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch } from "vue";
import { useRoute } from "vue-router";
import {
  ApiError,
  apiGet,
  apiPost,
  currentOperator,
  currentProject,
  humanizeError,
  setCurrentOperator,
} from "../api";
import PageHead from "../components/PageHead.vue";

interface Row {
  text_key: string;
  present_locales: string[];
  missing_locales: string[];
  status: Record<string, string>;
  // Additive in the current API. Optional fields let the UI survive a rolling service upgrade;
  // `assignees` accepts the plural spelling used by early preview builds.
  assignee?: Record<string, string>;
  assignees?: Record<string, string>;
}
interface Overview {
  locales: string[];
  keys: number;
  by_status: Record<string, number>;
  missing_total: number;
  rows: Row[];
}
interface LocEntry {
  text_key: string;
  locale: string;
  status: string;
  assignee: string;
}
interface AssignmentTarget {
  textKey: string;
  locale: string;
  status: string;
}
interface StatusTransition {
  to: string;
  label: string;
  direction: "forward" | "back";
}

const project = currentProject();
const route = useRoute();
const data = ref<Overview | null>(null);
const rowTotal = ref(0);
const nextRowCursor = ref<string | null>(null);
const loading = ref(false);
const rowsLoadingMore = ref(false);
const paginationError = ref("");
const transitioning = ref("");
const assigning = ref(false);
const error = ref("");
const flash = ref("");
const operator = ref(currentOperator());
const assignmentTarget = ref<AssignmentTarget | null>(null);
const assignee = ref("");
const focusedKey = ref("");
const activeFocusKey = ref("");
let loadEpoch = 0;

const STATUS_LABEL: Record<string, string> = {
  untranslated: "待译",
  translated: "已译",
  reviewing: "待校",
  final: "定稿",
};
const TRANSITIONS: Record<string, StatusTransition[]> = {
  untranslated: [{ to: "translated", label: "标为已译", direction: "forward" }],
  translated: [
    { to: "reviewing", label: "送校", direction: "forward" },
    { to: "untranslated", label: "退回待译", direction: "back" },
  ],
  reviewing: [
    { to: "final", label: "定稿", direction: "forward" },
    { to: "translated", label: "退回已译", direction: "back" },
  ],
  final: [{ to: "reviewing", label: "重开校对", direction: "back" }],
};

const assignmentHeading = computed(() => {
  const target = assignmentTarget.value;
  return target ? `${target.textKey} · ${target.locale}` : "";
});

function slot(key: string, locale: string): string {
  return `${key}\u0000${locale}`;
}

function rememberOperator(): string {
  const name = operator.value.trim();
  if (name) setCurrentOperator(name);
  return name;
}

function assignmentFor(key: string, locale: string): string {
  const row = data.value?.rows.find((candidate) => candidate.text_key === key);
  return row?.assignee?.[locale] ?? row?.assignees?.[locale] ?? "";
}

function focusQuery(): string {
  const value = route.query.focus;
  return typeof value === "string" ? value.trim() : "";
}

function rowDomId(key: string): string {
  return `localization-row-${encodeURIComponent(key)}`;
}

async function resolveFocusedRow(overview: Overview): Promise<void> {
  const requestedRef = focusQuery();
  focusedKey.value = "";
  if (!requestedRef) return;
  const rawId = requestedRef.replace(/^localized_text:/, "");
  let key = overview.rows.find((row) => row.text_key === rawId)?.text_key ?? "";
  if (!key) {
    // Global search deep-links with the canonical localized_text id, while this overview groups
    // rows by text_key. Resolve the exact hit once to bridge those two honest identifiers.
    try {
      const body = await apiGet<{
        hits: Array<{ ref: string; title: string }>;
      }>(
        `/projects/${encodeURIComponent(project)}/search?q=${encodeURIComponent(rawId)}`,
      );
      const hit = body.hits.find((candidate) => candidate.ref === requestedRef);
      if (hit && overview.rows.some((row) => row.text_key === hit.title)) key = hit.title;
    } catch {
      return;
    }
  }
  if (!key) return;
  focusedKey.value = key;
  await nextTick();
  document.getElementById(rowDomId(key))?.scrollIntoView({ behavior: "smooth", block: "center" });
}

async function focusKeyForRequest(): Promise<string> {
  const requestedRef = focusQuery();
  if (!requestedRef) return "";
  const rawId = requestedRef.replace(/^localized_text:/, "");
  try {
    const body = await apiGet<{ hits: Array<{ ref: string; title: string }> }>(
      `/projects/${encodeURIComponent(project)}/search?q=${encodeURIComponent(rawId)}`,
    );
    return body.hits.find((candidate) => candidate.ref === requestedRef)?.title ?? rawId;
  } catch {
    return rawId;
  }
}

async function fetchOverview(
  cursor: string | null,
  focusKey: string,
): Promise<{ overview: Overview; total?: number; next_cursor?: string | null }> {
  const params = new URLSearchParams({ limit: "40" });
  if (cursor) params.set("cursor", cursor);
  if (focusKey) params.set("focus", focusKey);
  return apiGet<{ overview: Overview; total?: number; next_cursor?: string | null }>(
    `/projects/${encodeURIComponent(project)}/localization?${params.toString()}`,
  );
}

async function load(): Promise<void> {
  const epoch = ++loadEpoch;
  loading.value = true;
  rowsLoadingMore.value = false;
  error.value = "";
  paginationError.value = "";
  try {
    const focusKey = await focusKeyForRequest();
    const body = await fetchOverview(null, focusKey);
    if (epoch !== loadEpoch) return;
    activeFocusKey.value = focusKey;
    data.value = body.overview;
    rowTotal.value = body.total ?? body.overview.rows.length;
    nextRowCursor.value = body.next_cursor ?? null;
    await resolveFocusedRow(body.overview);
  } catch (caught) {
    if (epoch === loadEpoch) error.value = humanizeError(caught);
  } finally {
    if (epoch === loadEpoch) loading.value = false;
  }
}


async function loadMoreRows(): Promise<void> {
  const cursor = nextRowCursor.value;
  const current = data.value;
  if (!cursor || !current || rowsLoadingMore.value) return;
  const epoch = loadEpoch;
  rowsLoadingMore.value = true;
  paginationError.value = "";
  try {
    const body = await fetchOverview(cursor, activeFocusKey.value);
    if (epoch !== loadEpoch || data.value !== current) return;
    const rows = [...current.rows, ...body.overview.rows];
    data.value = {
      ...body.overview,
      rows,
    };
    rowTotal.value = body.total ?? rows.length;
    nextRowCursor.value = body.next_cursor ?? null;
  } catch (caught) {
    if (epoch === loadEpoch && caught instanceof ApiError && [409, 422].includes(caught.status)) {
      rowsLoadingMore.value = false;
      await load();
      return;
    }
    if (epoch === loadEpoch) paginationError.value = humanizeError(caught);
  } finally {
    if (epoch === loadEpoch) rowsLoadingMore.value = false;
  }
}
onMounted(load);
watch(
  () => route.query.focus,
  () => {
    void load();
  },
);

async function transition(key: string, locale: string, to: string): Promise<void> {
  if (!to || transitioning.value || assigning.value) return;
  const by = rememberOperator();
  if (!by) {
    error.value = "请先填写当前操作人，状态记录需要留下署名。";
    return;
  }
  const targetSlot = slot(key, locale);
  transitioning.value = targetSlot;
  error.value = "";
  flash.value = "";
  try {
    await apiPost(`/projects/${encodeURIComponent(project)}/localization:transition`, {
      text_key: key,
      locale,
      to,
      by,
    });
    flash.value = `「${key}」${locale} 已切换到${STATUS_LABEL[to] ?? to}。`;
    await load();
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    transitioning.value = "";
  }
}

function openAssignment(key: string, locale: string, status: string): void {
  assignmentTarget.value = { textKey: key, locale, status };
  assignee.value = assignmentFor(key, locale) || operator.value.trim();
  error.value = "";
  flash.value = "";
}

function closeAssignment(): void {
  if (assigning.value) return;
  assignmentTarget.value = null;
  assignee.value = "";
}

async function saveAssignment(clear = false): Promise<void> {
  const target = assignmentTarget.value;
  if (!target || assigning.value || transitioning.value) return;
  const name = clear ? "" : assignee.value.trim();
  if (!clear && !name) {
    error.value = "请填写负责人姓名；若要取消现有指派，请使用「清空指派」。";
    return;
  }
  assigning.value = true;
  error.value = "";
  flash.value = "";
  try {
    const body = await apiPost<{ entry: LocEntry }>(
      `/projects/${encodeURIComponent(project)}/localization:assign`,
      {
        text_key: target.textKey,
        locale: target.locale,
        assignee: name,
      },
    );
    const row = data.value?.rows.find((candidate) => candidate.text_key === target.textKey);
    if (row) {
      row.assignee = {
        ...(row.assignee ?? row.assignees ?? {}),
        [target.locale]: body.entry.assignee,
      };
    }
    assignee.value = body.entry.assignee;
    target.status = body.entry.status;
    flash.value = body.entry.assignee
      ? `已将「${target.textKey}」${target.locale} 指派给 ${body.entry.assignee}。`
      : `已清空「${target.textKey}」${target.locale} 的负责人。`;
    // Assignment can create the first ledger entry for a locale, so refresh the status totals from
    // the authoritative overview rather than assuming that only the assignee changed.
    await load();
  } catch (caught) {
    error.value = humanizeError(caught);
  } finally {
    assigning.value = false;
  }
}
</script>

<template>
  <section>
    <PageHead
      overline="LOCALIZATION"
      title="本地化"
      purpose="翻译管理全流程：分派负责人，跟踪覆盖率，并让每条字符串走完 待译→已译→待校→定稿。"
    />

    <div class="pane toolbar">
      <label class="operator-field">
        <span>当前操作人</span>
        <input
          v-model="operator"
          maxlength="80"
          placeholder="填写姓名后可推进状态"
          @blur="rememberOperator"
          @keydown.enter="rememberOperator"
        />
      </label>
      <p class="muted small">状态推进会记录署名；负责人可以是你自己，也可以是其他译者。</p>
      <button class="ghost" :disabled="loading" @click="load">
        {{ loading ? "刷新中…" : "刷新" }}
      </button>
    </div>

    <p v-if="flash" class="flash" role="status">{{ flash }}</p>
    <p v-if="error" class="error" role="alert">{{ error }}</p>

    <div v-if="loading && !data" class="pane loading-state" aria-live="polite">
      正在整理语言覆盖与任务状态…
    </div>

    <template v-else-if="data">
      <div class="summary">
        <span class="s-chip">{{ data.keys }} 条文案 · {{ data.locales.length }} 语言</span>
        <span v-if="data.missing_total" class="s-chip warn">缺口 {{ data.missing_total }}</span>
        <span v-for="(n, st) in data.by_status" :key="st" class="s-chip">
          {{ STATUS_LABEL[st] ?? st }} {{ n }}
        </span>
      </div>

      <div v-if="assignmentTarget" class="pane assignment-panel">
        <div class="assignment-copy">
          <span class="eyebrow">ASSIGNMENT</span>
          <b>{{ assignmentHeading }}</b>
          <span class="muted small">
            当前状态：{{ STATUS_LABEL[assignmentTarget.status] ?? assignmentTarget.status }}
          </span>
        </div>
        <label class="assignee-field">
          <span>负责人</span>
          <input
            v-model="assignee"
            maxlength="80"
            placeholder="例如：林岚 / EN 外包组"
            :disabled="assigning"
            @keydown.enter="saveAssignment(false)"
          />
        </label>
        <div class="assignment-actions">
          <button class="primary" :disabled="assigning || !assignee.trim()" @click="saveAssignment(false)">
            {{ assigning ? "保存中…" : "保存指派" }}
          </button>
          <button
            class="ghost"
            :disabled="assigning"
            @click="saveAssignment(true)"
          >
            清空指派
          </button>
          <button class="ghost" :disabled="assigning" @click="closeAssignment">关闭</button>
        </div>
      </div>

      <div class="table-wrap pane">
        <table class="loc">
          <thead>
            <tr>
              <th>文案键</th>
              <th v-for="locale in data.locales" :key="locale">{{ locale }}</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in data.rows"
              :id="rowDomId(row.text_key)"
              :key="row.text_key"
              :class="{ focused: focusedKey === row.text_key }"
            >
              <td class="mono key">{{ row.text_key }}</td>
              <td v-for="locale in data.locales" :key="locale">
                <div class="cell-actions">
                  <span
                    class="st"
                    :class="row.status[locale]"
                  >
                    {{
                      transitioning === slot(row.text_key, locale)
                        ? "保存中…"
                        : (STATUS_LABEL[row.status[locale]] ?? row.status[locale])
                    }}
                  </span>
                  <button
                    v-for="action in TRANSITIONS[row.status[locale]] ?? []"
                    :key="action.to"
                    class="transition-btn"
                    :class="action.direction"
                    :disabled="Boolean(transitioning) || assigning"
                    :title="`${row.text_key} · ${locale}：${action.label}`"
                    @click="transition(row.text_key, locale, action.to)"
                  >
                    {{ action.label }}
                  </button>
                  <button
                    class="assign-btn"
                    :class="{
                      on:
                        assignmentTarget?.textKey === row.text_key &&
                        assignmentTarget?.locale === locale,
                    }"
                    :title="`为 ${row.text_key} · ${locale} 指派负责人`"
                    :disabled="assigning || Boolean(transitioning)"
                    @click="openAssignment(row.text_key, locale, row.status[locale])"
                  >
                    {{ assignmentFor(row.text_key, locale) || "指派" }}
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="data.rows.length" class="pager" aria-label="本地化文案分页">
        <span class="muted small">已显示 {{ data.rows.length }} / {{ rowTotal }} 条文案</span>
        <button
          v-if="nextRowCursor"
          class="ghost"
          :disabled="rowsLoadingMore || loading"
          @click="loadMoreRows"
        >
          {{ rowsLoadingMore ? "加载中…" : "加载下一页" }}
        </button>
        <span v-else class="muted small">已到末页</span>
        <span v-if="paginationError" class="error small" role="alert">{{ paginationError }}</span>
      </div>
      <p v-if="!data.rows.length" class="muted empty">当前世界还没有可管理的本地化文案。</p>
    </template>

    <div v-else class="pane empty-state">
      <p>暂时无法读取本地化任务。</p>
      <button class="ghost" :disabled="loading" @click="load">重新加载</button>
    </div>
  </section>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: flex-end;
  gap: 0.8rem;
  padding: 0.75rem 0.9rem;
}
.toolbar .small {
  flex: 1;
  margin: 0 0 0.12rem;
}
.operator-field,
.assignee-field {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  color: var(--ow-muted);
  font-size: 0.76rem;
}
.operator-field {
  width: min(16rem, 38vw);
}
input {
  min-width: 0;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  font: inherit;
  padding: 0.45rem 0.6rem;
}
input:focus {
  border-color: var(--ow-gold-soft);
  outline: none;
  box-shadow: 0 0 0 2px rgba(240, 210, 138, 0.08);
}
button {
  border-radius: var(--ow-control-radius);
  cursor: pointer;
  font: inherit;
}
button:disabled {
  cursor: not-allowed;
  opacity: 0.55;
}
button.ghost {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  padding: 0.45rem 0.8rem;
}
button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
  padding: 0.45rem 0.85rem;
}
.flash,
.error {
  margin: 0.75rem 0 0;
  font-size: 0.84rem;
}
.flash {
  color: #8ed4ac;
}
.error {
  color: #e89a9a;
}
.loading-state,
.empty-state {
  margin-top: 0.8rem;
  padding: 1.4rem;
  color: var(--ow-muted);
}
.empty-state p {
  margin-top: 0;
}
.summary {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  margin: 1rem 0;
}
.s-chip {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0,
    100% 0,
    100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%,
    0 100%,
    0 var(--ow-chip-nip)
  );
  color: var(--ow-ink-dim);
  font-size: 0.78rem;
  padding: 0.2rem 0.55rem;
}
.s-chip.warn {
  border-color: rgba(224, 101, 58, 0.45);
  color: var(--ow-flag, #e0653a);
}
.assignment-panel {
  display: grid;
  grid-template-columns: minmax(12rem, 1fr) minmax(14rem, 1fr) auto;
  gap: 0.9rem;
  align-items: end;
  margin-bottom: 0.8rem;
  padding: 0.85rem 1rem;
  border-color: var(--ow-gold-soft);
}
.assignment-copy {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  min-width: 0;
}
.assignment-copy b {
  overflow: hidden;
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
  text-overflow: ellipsis;
  white-space: nowrap;
}
.eyebrow {
  color: var(--ow-cyan);
  font-size: 0.65rem;
  letter-spacing: 0.13em;
}
.assignment-actions {
  display: flex;
  gap: 0.45rem;
}
.table-wrap {
  overflow-x: auto;
  padding: 0;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 0.7rem;
  margin-top: 0.7rem;
}
.loc {
  width: 100%;
  border-collapse: collapse;
  font-size: 0.82rem;
}
.loc th,
.loc td {
  min-width: 9.5rem;
  padding: 0.55rem 0.65rem;
  border-bottom: 1px solid var(--ow-line);
  text-align: left;
  vertical-align: top;
}
.loc th {
  color: var(--ow-muted);
  font-size: 0.72rem;
  font-weight: 500;
  letter-spacing: 0.05em;
}
.loc th:first-child,
.loc td:first-child {
  min-width: 12rem;
  position: sticky;
  left: 0;
  z-index: 1;
  background: rgba(13, 18, 42, 0.96);
}
.loc tbody tr:hover td {
  background-color: rgba(143, 214, 232, 0.025);
}
.loc tbody tr:hover td:first-child {
  background-color: rgba(18, 25, 54, 0.98);
}
.loc tbody tr.focused td {
  background-color: rgba(240, 210, 138, 0.08);
  box-shadow: inset 0 1px rgba(240, 210, 138, 0.26), inset 0 -1px rgba(240, 210, 138, 0.26);
}
.loc tbody tr.focused td:first-child {
  background-color: rgba(42, 38, 45, 0.98);
  color: var(--ow-gold-bright);
}
.loc .key {
  color: var(--ow-ink-dim);
  word-break: break-word;
}
.cell-actions {
  display: flex;
  align-items: center;
  gap: 0.35rem;
}
.st,
.transition-btn,
.assign-btn {
  white-space: nowrap;
}
.st {
  min-width: 3.8rem;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  color: var(--ow-ink);
  font-size: 0.74rem;
  padding: 0.2rem 0.5rem;
}
.st.untranslated {
  color: var(--ow-flag, #e0653a);
}
.st.translated {
  color: var(--ow-cyan);
}
.st.reviewing {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
}
.st.final {
  color: #6fcf97;
  border-color: #6fcf97;
}
.transition-btn {
  background: transparent;
  border: 1px solid rgba(143, 214, 232, 0.28);
  color: var(--ow-cyan);
  font-size: 0.68rem;
  padding: 0.18rem 0.35rem;
}
.transition-btn.back {
  border-color: rgba(224, 101, 58, 0.3);
  color: #e5a17f;
}
.assign-btn {
  max-width: 8rem;
  overflow: hidden;
  background: transparent;
  border: 1px solid transparent;
  color: var(--ow-muted);
  font-size: 0.7rem;
  padding: 0.18rem 0.35rem;
  text-overflow: ellipsis;
}
.assign-btn:hover,
.assign-btn.on {
  border-color: rgba(143, 214, 232, 0.35);
  color: var(--ow-cyan);
}
.small {
  font-size: 0.76rem;
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
}
.empty {
  padding: 2rem 0;
}
@media (max-width: 820px) {
  .toolbar,
  .assignment-panel {
    align-items: stretch;
    flex-direction: column;
  }
  .toolbar {
    display: flex;
  }
  .operator-field {
    width: 100%;
  }
  .assignment-panel {
    display: flex;
  }
  .assignment-actions {
    flex-wrap: wrap;
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
