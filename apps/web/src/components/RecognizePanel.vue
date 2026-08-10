<script setup lang="ts">
// Import & recognise a foreign game-project file → an editable plan → human review.
// Upload (incl. binary .xlsx) or paste; auto/forced format; per-column mapping editor with reusable
// templates; per-proposal keep/drop; optional §8-guarded LLM relations (default off). Nothing lands
// without a human approving the staged item in the Review queue.
import { computed, onMounted, ref } from "vue";
import { ApiError, apiGet, apiPost, apiDelete, currentProject, humanizeError } from "../api";
import FilePicker from "./FilePicker.vue";

interface ProposedEntity {
  id: string;
  name: string;
  type: string;
  method: string;
  source_ref?: { locator?: string } | null;
}
interface ProposedRelation {
  source: string;
  target: string;
  kind: string;
  method: string;
  evidence?: string;
}
interface ColumnMapping {
  id_column: string | null;
  name_column: string | null;
  type_column: string | null;
  description_column: string | null;
  relation_columns: Record<string, string>;
  ignore_columns: string[];
}
interface Plan {
  source_format: string;
  entities: ProposedEntity[];
  relations: ProposedRelation[];
  column_mapping: ColumnMapping | null;
  columns: string[];
  unmapped: string[];
  warnings: string[];
}
interface RecognizeResult {
  source_format: string;
  summary: Record<string, number>;
  plan: Plan;
  new: string[];
  changed: string[];
  unchanged: string[];
  warnings: string[];
  applied: boolean;
  review_item_id?: string | null;
  audit_preview?: { totals: Record<string, number> };
}
interface PageBounds {
  start: number;
  end: number;
}
interface IndexedRelation {
  relation: ProposedRelation;
  index: number;
}
interface MappingTemplatePage {
  templates: Record<string, ColumnMapping>;
  total: number;
  next_cursor: string | null;
  snapshot_etag: string;
}

const FORMATS = [
  { value: "auto", label: "自动识别格式（推荐）" },
  { value: "table", label: "表格 CSV / XLSX / JSON（陌生列）" },
  { value: "articy", label: "articy:draft 导出 JSON" },
  { value: "ink", label: "ink 叙事脚本" },
  { value: "yarn", label: "Yarn Spinner 脚本" },
  { value: "ue", label: "UE DataTable JSON" },
  { value: "unity", label: "Unity ScriptableObject JSON" },
];
const PROPOSAL_PAGE_SIZE = 25;
const TEMPLATE_PAGE_SIZE = 20;
const WARNING_PREVIEW_SIZE = 8;
type Role = "field" | "id" | "name" | "type" | "description" | "relation" | "ignore";
const ROLE_LABEL: Record<Role, string> = {
  field: "字段（存 metadata）",
  id: "ID",
  name: "名称",
  type: "类型",
  description: "描述",
  relation: "关系（外键）",
  ignore: "忽略",
};

const project = currentProject();
const file = ref<File | null>(null);
const fileB64 = ref("");
const pasteText = ref("");
const format = ref("auto");
const enableLlm = ref(false);

const busy = ref(false);
const error = ref("");
const result = ref<RecognizeResult | null>(null);

// editable per-column mapping (table only)
const roles = ref<Record<string, Role>>({});
const kinds = ref<Record<string, string>>({});
const mappingDirty = ref(false);

// drop individual proposals before applying
const dropEntities = ref<Set<string>>(new Set());
const dropRelations = ref<Set<number>>(new Set());
const entityPage = ref(1);
const relationPage = ref(1);
const reviewedEntityPages = ref<Set<number>>(new Set());
const reviewedRelationPages = ref<Set<number>>(new Set());
const warningsExpanded = ref(false);

// reusable mapping templates
const templates = ref<Record<string, ColumnMapping>>({});
const templateName = ref("");
const templateTotal = ref(0);
const nextTemplateCursor = ref<string | null>(null);
const templateSnapshot = ref("");
const templatesLoading = ref(false);
const templatesLoadingMore = ref(false);
const templateError = ref("");
let templateLoadSequence = 0;

const isTable = computed(() => result.value?.source_format === "table");
const hasInput = computed(() => !!fileB64.value || !!pasteText.value.trim());

function proposalPageCount(total: number): number {
  return Math.max(1, Math.ceil(total / PROPOSAL_PAGE_SIZE));
}

function pageBounds(total: number, page: number): PageBounds {
  if (total <= 0) return { start: 0, end: 0 };
  const safePage = Math.min(Math.max(1, page), proposalPageCount(total));
  const start = (safePage - 1) * PROPOSAL_PAGE_SIZE;
  return { start, end: Math.min(start + PROPOSAL_PAGE_SIZE, total) };
}

function entityPageItems(entities: ProposedEntity[], page: number): ProposedEntity[] {
  const { start, end } = pageBounds(entities.length, page);
  return entities.slice(start, end);
}

function relationPageItems(
  relations: ProposedRelation[],
  page: number,
): IndexedRelation[] {
  const { start, end } = pageBounds(relations.length, page);
  return relations.slice(start, end).map((relation, offset) => ({ relation, index: start + offset }));
}

function unreviewedItemCount(total: number, reviewedPages: ReadonlySet<number>): number {
  let reviewed = 0;
  for (const page of reviewedPages) {
    const { start, end } = pageBounds(total, page);
    reviewed += Math.max(0, end - start);
  }
  return Math.max(0, total - reviewed);
}

function planForSubmission(
  plan: Plan,
  droppedEntityIds: ReadonlySet<string>,
  droppedRelationIndexes: ReadonlySet<number>,
): Plan {
  return {
    ...plan,
    entities: plan.entities.filter((entity) => !droppedEntityIds.has(entity.id)),
    relations: plan.relations.filter((_, index) => !droppedRelationIndexes.has(index)),
  };
}

const entityPageCount = computed(() => proposalPageCount(result.value?.plan.entities.length ?? 0));
const relationPageCount = computed(() => proposalPageCount(result.value?.plan.relations.length ?? 0));
const visibleEntities = computed(() => entityPageItems(result.value?.plan.entities ?? [], entityPage.value));
const visibleRelations = computed(() => relationPageItems(result.value?.plan.relations ?? [], relationPage.value));
const entityBounds = computed(() => pageBounds(result.value?.plan.entities.length ?? 0, entityPage.value));
const relationBounds = computed(() => pageBounds(result.value?.plan.relations.length ?? 0, relationPage.value));
const unreviewedEntities = computed(() =>
  unreviewedItemCount(result.value?.plan.entities.length ?? 0, reviewedEntityPages.value),
);
const unreviewedRelations = computed(() =>
  unreviewedItemCount(result.value?.plan.relations.length ?? 0, reviewedRelationPages.value),
);
const unreviewedTotal = computed(() => unreviewedEntities.value + unreviewedRelations.value);
const canApplyPlan = computed(() =>
  Boolean(result.value && !result.value.applied && unreviewedTotal.value === 0),
);
const visibleWarnings = computed(() => {
  const warnings = result.value?.warnings ?? [];
  return warningsExpanded.value ? warnings : warnings.slice(0, WARNING_PREVIEW_SIZE);
});
const hiddenWarningCount = computed(() =>
  Math.max(0, (result.value?.warnings.length ?? 0) - visibleWarnings.value.length),
);

function markPageReviewed(kind: "entity" | "relation", page: number): void {
  const source = kind === "entity" ? reviewedEntityPages : reviewedRelationPages;
  const next = new Set(source.value);
  next.add(page);
  source.value = next;
}

function goToEntityPage(page: number): void {
  const next = Math.min(Math.max(1, page), entityPageCount.value);
  entityPage.value = next;
  markPageReviewed("entity", next);
}

function goToRelationPage(page: number): void {
  const next = Math.min(Math.max(1, page), relationPageCount.value);
  relationPage.value = next;
  markPageReviewed("relation", next);
}

function resetProposalReview(plan: Plan): void {
  entityPage.value = 1;
  relationPage.value = 1;
  reviewedEntityPages.value = new Set(plan.entities.length ? [1] : []);
  reviewedRelationPages.value = new Set(plan.relations.length ? [1] : []);
  warningsExpanded.value = false;
}

function onFile(f: File): void {
  file.value = f;
  const reader = new FileReader();
  reader.onload = () => {
    const r = reader.result as string;
    fileB64.value = r.includes(",") ? r.split(",", 2)[1] : r;
  };
  reader.readAsDataURL(f);
}

function buildMapping(): ColumnMapping | undefined {
  if (!isTable.value || !mappingDirty.value) return undefined;
  const m: ColumnMapping = {
    id_column: null,
    name_column: null,
    type_column: null,
    description_column: null,
    relation_columns: {},
    ignore_columns: [],
  };
  for (const [col, role] of Object.entries(roles.value)) {
    if (role === "id") m.id_column = col;
    else if (role === "name") m.name_column = col;
    else if (role === "type") m.type_column = col;
    else if (role === "description") m.description_column = col;
    else if (role === "relation") m.relation_columns[col] = kinds.value[col] || col;
    else if (role === "ignore") m.ignore_columns.push(col);
  }
  return m;
}

function seedRolesFromMapping(plan: Plan): void {
  if (mappingDirty.value) return; // keep the human's edits across re-runs
  const m = plan.column_mapping;
  const r: Record<string, Role> = {};
  const k: Record<string, string> = {};
  for (const col of plan.columns) {
    if (m?.id_column === col) r[col] = "id";
    else if (m?.name_column === col) r[col] = "name";
    else if (m?.type_column === col) r[col] = "type";
    else if (m?.description_column === col) r[col] = "description";
    else if (m && col in m.relation_columns) {
      r[col] = "relation";
      k[col] = m.relation_columns[col];
    } else if (m?.ignore_columns.includes(col)) r[col] = "ignore";
    else r[col] = "field";
  }
  roles.value = r;
  kinds.value = k;
}

async function preview(): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    const body: Record<string, unknown> = {
      source_format: format.value,
      filename: file.value?.name ?? `paste.${format.value === "auto" ? "txt" : format.value}`,
      enable_llm: enableLlm.value,
    };
    if (fileB64.value) body.content_base64 = fileB64.value;
    else body.content = pasteText.value;
    const m = buildMapping();
    if (m) body.field_mapping = m;
    const res = await apiPost<RecognizeResult>(`/projects/${project}/import:recognize`, body);
    result.value = res;
    dropEntities.value = new Set();
    dropRelations.value = new Set();
    resetProposalReview(res.plan);
    seedRolesFromMapping(res.plan);
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

function onRoleChange(): void {
  mappingDirty.value = true;
}

function toggleEntity(id: string): void {
  const s = new Set(dropEntities.value);
  s.has(id) ? s.delete(id) : s.add(id);
  dropEntities.value = s;
}
function toggleRelation(i: number): void {
  const s = new Set(dropRelations.value);
  s.has(i) ? s.delete(i) : s.add(i);
  dropRelations.value = s;
}

async function applyPlan(): Promise<void> {
  if (!result.value || !canApplyPlan.value) return;
  busy.value = true;
  error.value = "";
  try {
    const plan = planForSubmission(result.value.plan, dropEntities.value, dropRelations.value);
    result.value = await apiPost<RecognizeResult>(`/projects/${project}/import:apply`, { plan });
  } catch (e) {
    error.value = humanizeError(e);
  } finally {
    busy.value = false;
  }
}

async function loadTemplates(
  cursor: string | null = null,
  append = false,
  retryStale = true,
): Promise<void> {
  const sequence = ++templateLoadSequence;
  if (append) templatesLoadingMore.value = true;
  else templatesLoading.value = true;
  templateError.value = "";
  const query = new URLSearchParams({ limit: String(TEMPLATE_PAGE_SIZE) });
  if (cursor) query.set("cursor", cursor);
  try {
    const page = await apiGet<MappingTemplatePage>(
      `/projects/${project}/recognize/mappings?${query.toString()}`,
    );
    if (sequence !== templateLoadSequence) return;
    templates.value = append
      ? { ...templates.value, ...page.templates }
      : page.templates;
    templateTotal.value = page.total;
    nextTemplateCursor.value = page.next_cursor;
    templateSnapshot.value = page.snapshot_etag;
  } catch (e) {
    if (sequence !== templateLoadSequence) return;
    if (e instanceof ApiError && e.status === 409 && cursor && retryStale) {
      templates.value = {};
      nextTemplateCursor.value = null;
      templateSnapshot.value = "";
      await loadTemplates(null, false, false);
      return;
    }
    templateError.value = humanizeError(e);
  } finally {
    if (sequence === templateLoadSequence) {
      templatesLoading.value = false;
      templatesLoadingMore.value = false;
    }
  }
}

async function loadMoreTemplates(): Promise<void> {
  const cursor = nextTemplateCursor.value;
  if (!cursor || templatesLoading.value || templatesLoadingMore.value) return;
  await loadTemplates(cursor, true);
}

async function saveTemplate(): Promise<void> {
  const m = buildMapping();
  if (!m || !templateName.value.trim()) return;
  templateError.value = "";
  try {
    await apiPost<{ saved: string }>(
      `/projects/${project}/recognize/mappings?include_details=false`,
      { name: templateName.value.trim(), mapping: m },
    );
    templateName.value = "";
    await loadTemplates();
  } catch (e) {
    templateError.value = humanizeError(e);
  }
}
function loadTemplate(name: string): void {
  const m = templates.value[name];
  if (!m || !result.value) return;
  const r: Record<string, Role> = {};
  const k: Record<string, string> = {};
  for (const col of result.value.plan.columns) {
    if (m.id_column === col) r[col] = "id";
    else if (m.name_column === col) r[col] = "name";
    else if (m.type_column === col) r[col] = "type";
    else if (m.description_column === col) r[col] = "description";
    else if (col in m.relation_columns) {
      r[col] = "relation";
      k[col] = m.relation_columns[col];
    } else if (m.ignore_columns.includes(col)) r[col] = "ignore";
    else r[col] = "field";
  }
  roles.value = r;
  kinds.value = k;
  mappingDirty.value = true;
  preview();
}
async function removeTemplate(name: string): Promise<void> {
  templateError.value = "";
  try {
    await apiDelete<{ deleted: string }>(
      `/projects/${project}/recognize/mappings/${encodeURIComponent(name)}?include_details=false`,
    );
    await loadTemplates();
  } catch (e) {
    templateError.value = humanizeError(e);
  }
}

onMounted(() => void loadTemplates());
</script>

<template>
  <div class="pane block recog">
    <div class="section"><span class="t">导入识别（外部项目文件）</span></div>
    <p class="muted small">
      指给它看你现有的世界——表格 / articy / ink / Yarn / UE / Unity——它识别出实体与关系，生成
      <b>可人工修订</b>的导入计划。确定性优先；先「识别预览」核对、改列映射、勾掉不要的，再「识别并入审」。
      识别结果一律进审阅台，正典不会被直接覆盖。
    </p>

    <div class="controls">
      <FilePicker accept=".csv,.xlsx,.json,.ink,.yarn" hint="上传文件（.xlsx 等二进制必须上传）" @select="onFile" />
      <select v-model="format" class="fmt">
        <option v-for="f in FORMATS" :key="f.value" :value="f.value">{{ f.label }}</option>
      </select>
      <label class="llm" title="默认关；开启需已在「设置」接入模型。AI 推断的关系经 §8 护栏（闭世界+证据+受控词表）再进人审。">
        <input v-model="enableLlm" type="checkbox" />
        LLM 辅助推断关系（默认关）
      </label>
    </div>
    <textarea
      v-model="pasteText"
      class="paste"
      rows="4"
      placeholder="或在此粘贴文本内容（CSV / JSON / ink / Yarn）；上传文件时此处忽略"
    ></textarea>

    <div class="row">
      <button class="ghost" :disabled="busy || !hasInput" @click="preview">
        {{ busy ? "识别中…" : "识别预览" }}
      </button>
      <button
        class="primary"
        :disabled="busy || !canApplyPlan"
        :title="unreviewedTotal ? `还有 ${unreviewedTotal} 项位于尚未核对的分页中` : ''"
        @click="applyPlan"
      >
        {{ unreviewedTotal ? `还需核对 ${unreviewedTotal} 项` : "识别并入审" }}
      </button>
    </div>
    <p v-if="result && !result.applied && unreviewedTotal" class="review-gate" role="status">
      为避免未显示的内容被默认送审，请翻阅全部分页；尚未核对
      {{ unreviewedEntities }} 个实体、{{ unreviewedRelations }} 条关系。
    </p>
    <p v-else-if="result && !result.applied" class="review-ready" role="status">
      已核对全部分页；送审时会排除所有取消勾选的项目。
    </p>
    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="result" class="out">
      <div class="plan">
        <span class="tag new">新增 {{ result.new.length }}</span>
        <span class="tag changed">改动 {{ result.changed.length }}</span>
        <span class="tag unchanged">未变 {{ result.unchanged.length }}</span>
        <span class="tag">实体 {{ result.summary.entities }}</span>
        <span class="tag">关系 {{ result.summary.relations }}</span>
        <span v-if="result.summary.llm_relations" class="tag ai">含 AI {{ result.summary.llm_relations }}</span>
        <span class="muted small">识别为：{{ result.source_format }}</span>
      </div>

      <!-- column mapping editor (table only) -->
      <details v-if="isTable && result.plan.columns.length" open>
        <summary>列映射（自动猜，可改后「识别预览」重算）</summary>
        <div class="maps">
          <div v-for="col in result.plan.columns" :key="col" class="maprow">
            <span class="col mono">{{ col }}</span>
            <select v-model="roles[col]" @change="onRoleChange">
              <option v-for="(label, role) in ROLE_LABEL" :key="role" :value="role">{{ label }}</option>
            </select>
            <input
              v-if="roles[col] === 'relation'"
              v-model="kinds[col]"
              class="kind"
              :placeholder="col"
              @input="onRoleChange"
            />
          </div>
        </div>
        <div class="tmpl">
          <input v-model="templateName" class="kind" placeholder="存为映射模板，输入名称…" />
          <button class="ghost xs" :disabled="!templateName.trim()" @click="saveTemplate">存为模板</button>
          <template v-for="(_, name) in templates" :key="name">
            <span class="chip">
              <button class="link" @click="loadTemplate(name)">{{ name }}</button>
              <button class="x" title="删除模板" @click="removeTemplate(name)">×</button>
            </span>
          </template>
          <span v-if="templatesLoading" class="muted small">正在读取模板…</span>
          <span v-else-if="templateTotal" class="muted small">
            已加载 {{ Object.keys(templates).length }} / {{ templateTotal }}
          </span>
          <button
            v-if="nextTemplateCursor"
            type="button"
            class="ghost xs"
            :disabled="templatesLoadingMore"
            @click="loadMoreTemplates"
          >
            {{ templatesLoadingMore ? "加载中…" : "加载更多模板" }}
          </button>
          <span v-if="templateSnapshot" class="template-snapshot mono" :title="templateSnapshot">
            快照 {{ templateSnapshot.slice(0, 8) }}
          </span>
        </div>
        <p v-if="templateError" class="error template-error" role="alert">{{ templateError }}</p>
      </details>

      <!-- entities: keep/drop -->
      <details v-if="result.plan.entities.length" open>
        <summary>
          识别出的实体（{{ result.plan.entities.length }}）— 已取消 {{ dropEntities.size }}，尚未核对
          {{ unreviewedEntities }}
        </summary>
        <div class="pager" aria-live="polite">
          <span>
            当前 {{ entityBounds.start + 1 }}–{{ entityBounds.end }} / {{ result.plan.entities.length }} ·
            第 {{ entityPage }} / {{ entityPageCount }} 页
          </span>
          <div class="pager-actions">
            <button type="button" class="ghost xs" :disabled="entityPage === 1" @click="goToEntityPage(entityPage - 1)">上一页</button>
            <button type="button" class="ghost xs" :disabled="entityPage === entityPageCount" @click="goToEntityPage(entityPage + 1)">下一页</button>
          </div>
        </div>
        <div class="items">
          <label
            v-for="e in visibleEntities"
            :key="e.id"
            class="item"
            :class="{ off: dropEntities.has(e.id) }"
          >
            <input type="checkbox" :checked="!dropEntities.has(e.id)" @change="toggleEntity(e.id)" />
            <b>{{ e.name }}</b>
            <span class="mono">{{ e.id }}</span>
            <span class="muted">{{ e.type }}</span>
            <span class="mono src">{{ e.source_ref?.locator || "" }}</span>
            <span class="badge" :class="e.method">{{ e.method === "llm" ? "AI" : "确定性" }}</span>
          </label>
        </div>
      </details>

      <!-- relations: keep/drop -->
      <details v-if="result.plan.relations.length" open>
        <summary>
          识别出的关系（{{ result.plan.relations.length }}）— 已取消 {{ dropRelations.size }}，尚未核对
          {{ unreviewedRelations }}
        </summary>
        <div class="pager" aria-live="polite">
          <span>
            当前 {{ relationBounds.start + 1 }}–{{ relationBounds.end }} / {{ result.plan.relations.length }} ·
            第 {{ relationPage }} / {{ relationPageCount }} 页
          </span>
          <div class="pager-actions">
            <button type="button" class="ghost xs" :disabled="relationPage === 1" @click="goToRelationPage(relationPage - 1)">上一页</button>
            <button type="button" class="ghost xs" :disabled="relationPage === relationPageCount" @click="goToRelationPage(relationPage + 1)">下一页</button>
          </div>
        </div>
        <div class="items">
          <label
            v-for="entry in visibleRelations"
            :key="entry.index"
            class="item"
            :class="{ off: dropRelations.has(entry.index) }"
          >
            <input type="checkbox" :checked="!dropRelations.has(entry.index)" @change="toggleRelation(entry.index)" />
            <span class="mono">{{ entry.relation.source }}</span>
            <span class="arrow">→</span>
            <span class="mono">{{ entry.relation.target }}</span>
            <span class="muted">{{ entry.relation.kind }}</span>
            <span v-if="entry.relation.evidence" class="muted ev">「{{ entry.relation.evidence }}」</span>
            <span class="badge" :class="entry.relation.method">{{ entry.relation.method === "llm" ? "AI" : "确定性" }}</span>
          </label>
        </div>
      </details>

      <p v-if="result.plan.unmapped.length" class="muted small">
        未映射字段（已存入 metadata，不丢失）：{{ result.plan.unmapped.join("、") }}
      </p>
      <ul v-if="result.warnings.length" class="warns">
        <li v-for="(w, i) in visibleWarnings" :key="i">{{ w }}</li>
      </ul>
      <button
        v-if="result.warnings.length > WARNING_PREVIEW_SIZE"
        type="button"
        class="ghost xs warning-toggle"
        :aria-expanded="warningsExpanded"
        @click="warningsExpanded = !warningsExpanded"
      >
        {{ warningsExpanded ? "收起额外警告" : `展开剩余 ${hiddenWarningCount} 条警告` }}
      </button>

      <div v-if="result.applied" class="applied">
        <span v-if="result.review_item_id" class="muted small">✓ 已送审阅台（去「审阅」页采纳后才入正典）。</span>
        <span v-else class="muted small">没有需要送审的新增/改动。</span>
        <span v-if="result.audit_preview" class="muted small audit">
          落库前审计预览：错误 {{ result.audit_preview.totals.error || 0 }} ·
          警告 {{ result.audit_preview.totals.warning || 0 }} ·
          提示 {{ result.audit_preview.totals.info || 0 }}
        </span>
      </div>
    </div>
  </div>
</template>

<style scoped>
.recog {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}
.small {
  font-size: 0.8rem;
}
.controls {
  display: flex;
  flex-wrap: wrap;
  gap: 0.6rem;
  align-items: center;
}
.fmt,
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.4rem 0.55rem;
  font: inherit;
  font-size: 0.82rem;
}
.llm {
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.8rem;
  color: var(--ow-ink-dim);
}
.paste,
.kind {
  width: 100%;
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.6rem;
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.8rem;
  resize: vertical;
}
.kind {
  width: auto;
  flex: 1;
  min-width: 8rem;
}
.row {
  display: flex;
  gap: 0.5rem;
}
.review-gate,
.review-ready {
  margin: 0;
  font-size: 0.78rem;
}
.review-gate {
  color: #d8b87a;
}
.review-ready {
  color: var(--ow-cyan);
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
button.xs {
  font-size: 0.78rem;
  padding: 0.3rem 0.6rem;
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
}
.out {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}
.plan {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: center;
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
.tag.ai {
  color: #c9a6e8;
  border-color: rgba(180, 140, 220, 0.5);
}
details {
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.4rem 0.7rem;
}
summary {
  cursor: pointer;
  font-size: 0.82rem;
  color: var(--ow-gold-bright);
}
.maps {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-top: 0.5rem;
}
.maprow {
  display: flex;
  gap: 0.5rem;
  align-items: center;
}
.maprow .col {
  min-width: 8rem;
  color: var(--ow-ink);
}
.tmpl {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
  margin-top: 0.55rem;
}
.template-snapshot {
  color: var(--ow-ink-dim);
  font-size: 0.68rem;
}
.template-error {
  margin: 0.4rem 0 0;
  font-size: 0.76rem;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 0.25rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  padding: 0.1rem 0.3rem 0.1rem 0.5rem;
}
.chip .link {
  border: none;
  background: none;
  color: var(--ow-cyan);
  padding: 0.1rem 0;
}
.chip .x {
  border: none;
  background: none;
  color: var(--ow-ink-dim);
  padding: 0 0.2rem;
}
.items {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  margin-top: 0.45rem;
}
.pager {
  display: flex;
  align-items: center;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 0.45rem;
  margin-top: 0.5rem;
  color: var(--ow-ink-dim);
  font-size: 0.76rem;
}
.pager-actions {
  display: flex;
  gap: 0.35rem;
}
.pager-actions button {
  min-height: 36px;
}
.item {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  align-items: baseline;
  font-size: 0.82rem;
  padding: 0.22rem 0;
  border-bottom: 1px solid rgba(255, 255, 255, 0.04);
}
.item.off {
  opacity: 0.4;
  text-decoration: line-through;
}
.item b {
  color: var(--ow-ink);
}
.src {
  font-size: 0.72rem;
  color: var(--ow-ink-dim);
}
.ev {
  font-style: italic;
}
.arrow {
  color: var(--ow-gold-bright);
}
.mono {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.78rem;
  color: var(--ow-cyan);
}
.badge {
  margin-left: auto;
  border-radius: 0.4rem;
  padding: 0.1rem 0.45rem;
  font-size: 0.72rem;
  border: 1px solid var(--ow-line);
}
.badge.deterministic {
  color: var(--ow-cyan);
  border-color: rgba(120, 200, 220, 0.45);
}
.badge.llm {
  color: #c9a6e8;
  border-color: rgba(180, 140, 220, 0.5);
}
.warns {
  margin: 0;
  padding-left: 1.1rem;
  font-size: 0.78rem;
  color: #d8b87a;
}
.warning-toggle {
  align-self: flex-start;
}
@media (max-width: 640px) {
  .pager {
    align-items: stretch;
    flex-direction: column;
  }
  .pager-actions button {
    min-height: 44px;
    flex: 1;
  }
}
.applied {
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
}
.applied .audit {
  color: var(--ow-cyan);
}
</style>
