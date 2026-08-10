<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, toRef, toRefs } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import PageHead from "../components/PageHead.vue";
import ReferenceReport from "../components/ReferenceReport.vue";
import { ApiError, apiGet, currentProject, humanizeError, llmConfig, llmParams } from "../api";
import { getJobChannel, resumeJob, startJob } from "../jobs";
import { example } from "../examples";

const phAngle = example("expandAngle");

// The real expansion chain (focus→pois→cast→quests), grounded on existing canon.
const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "retrieving", label: "检索正典" },
  { key: "expand_focus", label: "定位" },
  { key: "expand_pois", label: "地点" },
  { key: "expand_cast", label: "角色" },
  { key: "expand_quests", label: "支线" },
  { key: "assembling", label: "归整" },
  { key: "done", label: "候批" },
];

// evocative per-stage lines for the wait (animation copy)
const FLAVORS: Record<string, string> = {
  accepted: "受理扩写请求…",
  retrieving: "回看既有正典…",
  expand_focus: "读出焦点的张力…",
  expand_pois: "在地图上点亮新地点…",
  expand_cast: "请来几位次要角色…",
  expand_quests: "把支线接上主线…",
  assembling: "归整、校对、收口…",
  done: "扩写已成形。",
};

interface FocusOption {
  value: string;
  label: string;
  group: string;
}

type FocusArchiveKind = "region" | "entity" | "quest";

const FOCUS_PAGE_SIZE = 40;
const FOCUS_SOURCES: readonly { key: FocusArchiveKind; label: string }[] = [
  { key: "region", label: "区域" },
  { key: "entity", label: "阵营" },
  { key: "quest", label: "主线任务" },
];

interface SeedEntity {
  id: string;
  name: string;
  type: string;
  description: string;
}

interface PoiRow {
  id: string;
  name: string;
  region_id?: string;
  controlling_faction?: string;
  purpose?: string;
}

interface QuestRow {
  id: string;
  title: string;
  giver_npc?: string;
  location?: string;
  objective?: string;
}

interface Grounding {
  canon_anchor: string;
  grounded_refs: number;
  dangling_refs: string[];
  unspecified_refs: string[];
  canon_ids_referenced: string[];
}

interface ReferenceReportRow {
  source_ref: string;
  source_title: string;
  used_for: string;
  transformation: string;
  excluded: string[];
}

const focusOptionsByKind = reactive<Record<FocusArchiveKind, FocusOption[]>>({
  region: [],
  entity: [],
  quest: [],
});
const focusNextCursor = reactive<Record<FocusArchiveKind, string | null>>({
  region: null,
  entity: null,
  quest: null,
});
const focusTotal = reactive<Record<FocusArchiveKind, number>>({ region: 0, entity: 0, quest: 0 });
const focusScanned = reactive<Record<FocusArchiveKind, number>>({ region: 0, entity: 0, quest: 0 });
const focusLoading = reactive<Record<FocusArchiveKind, boolean>>({
  region: false,
  entity: false,
  quest: false,
});
const focusErrors = reactive<Record<FocusArchiveKind, string>>({ region: "", entity: "", quest: "" });
const focusLoadSequence: Record<FocusArchiveKind, number> = { region: 0, entity: 0, quest: 0 };
const focusOptions = computed(() =>
  FOCUS_SOURCES.flatMap(({ key }) => focusOptionsByKind[key]),
);
const loadError = computed(() => Object.values(focusErrors).filter(Boolean).join("；"));
const focusInitialized = ref(false);
const llmReady = ref(llmConfig().ready);

const form = ref({
  focus: "",
  angle: "",
  useReferences: true,
  pois: 3,
  npcs: 4,
  quests: 3,
});

interface ExpandResult {
  focusLabel: string;
  angle: string;
  counts: Record<string, number>;
  grounding: Grounding;
  pois: PoiRow[];
  npcs: SeedEntity[];
  quests: QuestRow[];
  trail: { verdict: string; gap_count: number }[];
  densityNote: string;
  referenceReport: ReferenceReportRow[];
}

const job = getJobChannel<ExpandResult>("world_expand", STAGES);
const { running, stageIndex, elapsed, result } = toRefs(job);
const lastCost = toRef(job, "cost");

const COUNT_LABELS: Record<string, string> = {
  entities: "新增实体",
  pois: "新增地点",
  quests: "新增支线",
  relations: "关系",
};

const focusGroups = computed(() => {
  const groups: Record<string, FocusOption[]> = {};
  for (const option of focusOptions.value) (groups[option.group] ??= []).push(option);
  return groups;
});

const hasFocus = computed(() => focusOptions.value.length > 0);
const canRun = computed(
  () => !!form.value.focus && !running.value && llmReady.value && hasFocus.value,
);

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}

interface FocusArchivePage {
  items: Array<Record<string, unknown>>;
  total: number;
  next_cursor: string | null;
}

function focusOption(kind: FocusArchiveKind, row: Record<string, unknown>): FocusOption | null {
  const id = String(row.id ?? "").trim();
  if (!id) return null;
  if (kind === "entity") {
    if (row.type !== "faction") return null;
    return {
      value: `faction:${id}`,
      label: String(row.name || id),
      group: "阵营",
    };
  }
  if (kind === "region") {
    return { value: `region:${id}`, label: String(row.name || id), group: "区域" };
  }
  return { value: `quest:${id}`, label: String(row.title || id), group: "主线任务" };
}

async function loadFocusPage(
  kind: FocusArchiveKind,
  cursor = "",
  append = false,
): Promise<void> {
  if (append && focusLoading[kind]) return;
  const sequence = ++focusLoadSequence[kind];
  focusLoading[kind] = true;
  focusErrors[kind] = "";
  try {
    const params = new URLSearchParams({ kind, limit: String(FOCUS_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    const body = await apiGet<FocusArchivePage>(
      `/projects/${currentProject()}/archive?${params.toString()}`,
    );
    if (sequence !== focusLoadSequence[kind]) return;
    const pageOptions = body.items
      .map((row) => focusOption(kind, row))
      .filter((option): option is FocusOption => option !== null);
    const rows = append ? [...focusOptionsByKind[kind], ...pageOptions] : pageOptions;
    focusOptionsByKind[kind] = [
      ...new Map(rows.map((option) => [option.value, option])).values(),
    ];
    focusTotal[kind] = body.total;
    focusScanned[kind] = append ? focusScanned[kind] + body.items.length : body.items.length;
    focusNextCursor[kind] = body.next_cursor;
    if (focusInitialized.value && !form.value.focus && focusOptions.value.length) {
      form.value.focus = focusOptions.value[0].value;
    }
  } catch (error) {
    if (sequence !== focusLoadSequence[kind]) return;
    if (cursor && error instanceof ApiError && error.status === 409) {
      await loadFocusPage(kind);
      return;
    }
    focusErrors[kind] = humanizeError(error);
  } finally {
    if (sequence === focusLoadSequence[kind]) focusLoading[kind] = false;
  }
}

async function loadMoreFocus(kind: FocusArchiveKind): Promise<void> {
  const cursor = focusNextCursor[kind];
  if (!cursor) return;
  await loadFocusPage(kind, cursor, true);
}

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  void resumeJob<ExpandResult>("world_expand", {
    stages: STAGES,
    onEvent: onExpandEvent,
    parseResult: parseExpandResult,
  });
  await Promise.all(FOCUS_SOURCES.map(({ key }) => loadFocusPage(key)));
  if (!form.value.focus && focusOptions.value.length) form.value.focus = focusOptions.value[0].value;
  focusInitialized.value = true;
});

onUnmounted(() => {
  for (const { key } of FOCUS_SOURCES) focusLoadSequence[key] += 1;
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});

function onExpandEvent(
  ch: { stageIndex: number },
  event: { type: string; data: Record<string, unknown> },
): void {
  if (event.type !== "stage") return;
  const name = String(event.data.name ?? "");
  const index = STAGES.findIndex((stage) => stage.key === name);
  if (index > ch.stageIndex) ch.stageIndex = index;
}

function parseExpandResult(raw: Record<string, unknown>): ExpandResult {
  const source = raw as {
    focus_label?: string;
    angle?: string;
    counts?: Record<string, number>;
    grounding?: Grounding;
    density?: { note?: string };
    refine_trail?: { verdict: string; gap_count: number }[];
    reference_report?: ReferenceReportRow[];
    bundle?: {
      entities?: Record<string, SeedEntity>;
      pois?: Record<string, PoiRow>;
      quests?: Record<string, QuestRow>;
    };
  };
  const bundle = source.bundle ?? {};
  const names = new Map(Object.entries(bundle.entities ?? {}).map(([id, entity]) => [id, entity.name]));
  const label = (id?: string): string => (id ? (names.get(id) ?? id) : "—");
  return {
    focusLabel: source.focus_label ?? "",
    angle: source.angle ?? "",
    counts: source.counts ?? {},
    grounding: source.grounding ?? {
      canon_anchor: "",
      grounded_refs: 0,
      dangling_refs: [],
      unspecified_refs: [],
      canon_ids_referenced: [],
    },
    pois: Object.values(bundle.pois ?? {}).map((poi) => ({
      ...poi,
      controlling_faction: label(poi.controlling_faction),
      region_id: label(poi.region_id),
    })),
    npcs: Object.values(bundle.entities ?? {}).filter((entity) => entity.type === "npc"),
    quests: Object.values(bundle.quests ?? {}).map((quest) => ({
      ...quest,
      giver_npc: label(quest.giver_npc),
      location: label(quest.location),
    })),
    trail: source.refine_trail ?? [],
    densityNote: source.density?.note ?? "",
    referenceReport: source.reference_report ?? [],
  };
}

async function run(): Promise<void> {
  if (!canRun.value) return;
  await startJob<ExpandResult>("world_expand", {
    kind: "world_expand",
    stages: STAGES,
    params: {
      ...llmParams(),
      brief: {
        focus_ref: form.value.focus,
        angle: form.value.angle.trim(),
        use_references: form.value.useReferences,
        poi_count: form.value.pois,
        npc_count: form.value.npcs,
        quest_count: form.value.quests,
      },
    },
    onEvent: onExpandEvent,
    parseResult: parseExpandResult,
  });
}
</script>

<template>
  <section>
    <PageHead overline="EXPAND" title="扩写工坊" purpose="锚定一个焦点，长出接地于既有设定的新内容。" />

    <p v-if="!focusInitialized" class="muted empty">正在分批读取可用焦点…</p>
    <p v-if="loadError" class="error">部分焦点读取失败：{{ loadError }}</p>

    <div v-if="focusInitialized" class="focus-pager" aria-label="扩写焦点分页">
      <div v-for="source in FOCUS_SOURCES" :key="source.key" class="focus-page">
        <span class="muted">
          {{ source.label }}：已载入 {{ focusOptionsByKind[source.key].length }} 项
          <template v-if="source.key === 'entity'">（扫描 {{ focusScanned[source.key] }} / {{ focusTotal[source.key] }} 条实体）</template>
          <template v-else>/ {{ focusTotal[source.key] }}</template>
        </span>
        <button
          v-if="focusNextCursor[source.key]"
          type="button"
          class="ghost"
          :disabled="focusLoading[source.key]"
          @click="loadMoreFocus(source.key)"
        >
          {{ focusLoading[source.key] ? "载入中…" : `加载更多${source.label}` }}
        </button>
      </div>
    </div>

    <p v-if="focusInitialized && !hasFocus" class="muted empty">
      这个世界还没有可作焦点的区域 / 阵营 / 主线。先到
      <RouterLink to="/genesis" class="golink">创世工坊</RouterLink>
      开辟一个种子世界，再来扩写。
    </p>

    <div v-if="hasFocus" class="pane form">
      <label class="field">
        <span class="label">扩写焦点 <em>必选</em></span>
        <select v-model="form.focus">
          <optgroup v-for="(options, group) in focusGroups" :key="group" :label="group">
            <option v-for="option in options" :key="option.value" :value="option.value">
              {{ option.label }}
            </option>
          </optgroup>
        </select>
      </label>

      <label class="field">
        <span class="label">扩写角度 <i class="muted">可留空，模型会自行读出焦点的张力</i></span>
        <textarea
          v-model="form.angle"
          rows="2"
          maxlength="2000"
          :placeholder="`例如：${phAngle}`"
        ></textarea>
      </label>

      <label class="ground-row">
        <input v-model="form.useReferences" type="checkbox" />
        <span>借鉴灵感库 <i class="muted">只取相关片段作灵感，不复制原文</i></span>
      </label>

      <div class="field">
        <span class="label">生成规模 <i class="muted">0 = 不要这一类</i></span>
        <div class="scales">
          <label v-for="(label, key) in { pois: '地点', npcs: '次要角色', quests: '支线' }" :key="key">
            <span class="muted">{{ label }} {{ form[key] }}</span>
            <input v-model.number="form[key]" type="range" min="0" :max="key === 'npcs' ? 16 : 12" />
          </label>
        </div>
      </div>

      <button class="primary" :disabled="!canRun" @click="run">
        {{ running ? "正在生长…" : "开始扩写" }}
      </button>
      <p v-if="!llmReady" class="muted small">
        请先在
        <RouterLink to="/settings" class="golink">设置</RouterLink>
        接入模型。
      </p>
    </div>

    <StepperProgress
      v-if="running || stageIndex >= 0"
      :stages="STAGES"
      :index="stageIndex"
      :running="running"
      :elapsed="elapsed"
      :flavors="FLAVORS"
      hint="扩写通常需要一两分钟"
    />

    <div v-if="result" class="pane done reveal">
      <p class="summary">
        围绕 <b>{{ result.focusLabel }}</b> 的扩写<span v-if="result.angle">：{{ result.angle }}</span>
      </p>
      <div class="chips">
        <span v-for="(count, key) in result.counts" :key="key" class="chip static">
          {{ COUNT_LABELS[key] ?? key }} <b>{{ count }}</b>
        </span>
        <span
          class="chip static ground"
          :class="{
            ok:
              result.grounding.dangling_refs.length === 0 &&
              result.grounding.unspecified_refs.length === 0,
          }"
        >
          已接地引用 <b>{{ result.grounding.grounded_refs }}</b>
          · 悬空 <b>{{ result.grounding.dangling_refs.length }}</b>
          · 未指定 <b>{{ result.grounding.unspecified_refs.length }}</b>
        </span>
        <span v-if="lastCost" class="chip static">本次 <b>${{ lastCost.toFixed(4) }}</b></span>
      </div>
      <p v-if="result.grounding.canon_ids_referenced.length" class="muted refs">
        引用既有 id：{{ result.grounding.canon_ids_referenced.join("、") }}
      </p>
      <p v-if="result.grounding.dangling_refs.length" class="warn">
        悬空引用（需审阅修正）：{{ result.grounding.dangling_refs.join("、") }}
      </p>
      <p v-if="result.grounding.unspecified_refs.length" class="warn">
        未指定的引用（模型留空、已自动锚到焦点，建议审阅时补全）：{{
          result.grounding.unspecified_refs.join("、")
        }}
      </p>
      <p v-if="result.densityNote" class="warn">{{ result.densityNote }}</p>

      <ReferenceReport :rows="result.referenceReport" />

      <template v-if="result.pois.length">
        <div class="section"><span class="t">新增地点</span></div>
        <div class="cards">
          <div v-for="poi in result.pois" :key="poi.id" class="card">
            <b>{{ poi.name }}</b>
            <span class="muted tag">区域 {{ poi.region_id }} · 控制 {{ poi.controlling_faction }}</span>
            <span class="muted">{{ poi.purpose }}</span>
          </div>
        </div>
      </template>

      <template v-if="result.npcs.length">
        <div class="section"><span class="t">次要角色</span></div>
        <div class="cards">
          <div v-for="person in result.npcs" :key="person.id" class="card">
            <b>{{ person.name }}</b>
            <span class="muted">{{ person.description }}</span>
          </div>
        </div>
      </template>

      <template v-if="result.quests.length">
        <div class="section"><span class="t">支线任务</span></div>
        <div class="cards">
          <div v-for="quest in result.quests" :key="quest.id" class="card">
            <b>{{ quest.title }}</b>
            <span class="muted tag">交予 {{ quest.giver_npc }} · 地点 {{ quest.location }}</span>
            <span class="muted">{{ quest.objective }}</span>
          </div>
        </div>
      </template>

      <p v-if="result.trail.length" class="muted refs">
        自评精修：{{ result.trail.map((r) => `${r.verdict === "pass" ? "通过" : "需精修"}(缺口${r.gap_count})`).join(" → ") }}
      </p>
      <p class="muted">新内容已入审阅台，采纳后并入既有世界。</p>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
  line-height: 1.6;
}

.empty {
  padding: 0.6rem 0;
}

.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 1rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.focus-pager {
  display: grid;
  gap: 0.45rem;
  padding: 0.6rem 0.7rem;
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  background: var(--ow-panel-2);
}

.focus-page {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  font-size: 0.76rem;
}

.focus-page .ghost {
  min-height: 32px;
  white-space: nowrap;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
  letter-spacing: 0.04em;
}

.label em {
  color: var(--ow-gold-bright);
  font-style: normal;
  font-size: 0.72rem;
  margin-left: 0.3rem;
}

.label i {
  font-style: normal;
  font-size: 0.72rem;
  margin-left: 0.3rem;
}

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

textarea {
  resize: vertical;
}

select:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.ground-row {
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  font-size: 0.85rem;
  line-height: 1.5;
  cursor: pointer;
}

.ground-row input[type="checkbox"] {
  margin-top: 0.2rem;
}

.ground-row i {
  font-style: normal;
  font-size: 0.76rem;
}

.scales {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.7rem;
}

.scales label {
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.8rem;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: var(--ow-control-radius);
  color: #241a05;
  font-weight: 600;
  padding: 0.6rem 1rem;
  cursor: pointer;
  box-shadow: 0 0 12px rgba(217, 181, 108, 0.2);
  transition:
    transform 0.12s ease,
    box-shadow 0.15s ease,
    filter 0.15s ease;
}

button.primary:hover:not(:disabled) {
  transform: translateY(-1px);
  filter: brightness(1.05);
  box-shadow: 0 0 18px rgba(240, 210, 138, 0.35);
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.small {
  font-size: 0.78rem;
}

.error {
  color: #e89a9a;
}

.warn {
  color: #e6c07e;
  font-size: 0.82rem;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.summary {
  margin: 0 0 0.5rem;
  line-height: 1.7;
}

.summary b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
  margin-bottom: 0.4rem;
}

.chip.static {
  border: 1px solid var(--ow-line);
  background: rgba(16, 22, 48, 0.6);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-muted);
  font-size: 0.8rem;
  padding: 0.22rem 0.7rem;
  cursor: default;
}

.chip.static b {
  color: var(--ow-ink);
}

.chip.ground.ok {
  border-color: rgba(142, 212, 172, 0.5);
  color: #8ed4ac;
}

.chip.ground.ok b {
  color: #8ed4ac;
}

.refs {
  font-size: 0.8rem;
  margin: 0.1rem 0 0.4rem;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 0.6rem;
  margin-bottom: 0.4rem;
}

.card {
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  background: var(--ow-panel-2);
  padding: 0.55rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.85rem;
}

.card b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.card .tag {
  font-size: 0.74rem;
  color: var(--ow-cyan);
}
</style>
