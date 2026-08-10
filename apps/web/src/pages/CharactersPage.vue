<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref, toRef, toRefs } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import PageHead from "../components/PageHead.vue";
import Modal from "../components/Modal.vue";
import {
  ApiError,
  humanizeError,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  currentProject,
  llmConfig,
  llmParams,
} from "../api";
import { getJobChannel, resumeJob, startJob } from "../jobs";
import { notifyError } from "../toast";
import { example } from "../examples";
import { askConfirm } from "../confirm";

const phConcept = example("characterConcept");

const UNDECIDED = "暂未想好";
const CUSTOM = "其他…";

const ROLE_OPTIONS = ["主角", "同伴", "导师", "反派", "中立商人", "任务发布者", "线人", "守门人"];

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "retrieving", label: "检索" },
  { key: "generating", label: "推演" },
  { key: "parsing", label: "整理" },
  { key: "done", label: "候批" },
];

// evocative per-stage lines for the wait (animation copy)
const FLAVORS: Record<string, string> = {
  accepted: "受理角色请求…",
  retrieving: "翻检相关设定…",
  generating: "为这个人注入血肉与口吻…",
  parsing: "整理成一张角色卡…",
  done: "角色已成形。",
};

const PROFILE_LABELS: Record<string, string> = {
  appearance: "外貌",
  personality: "性格",
  backstory: "背景故事",
  motivation: "动机与目标",
  abilities: "能力与专长",
  weakness: "弱点与恐惧",
  voice: "说话方式",
};

interface ArchiveEntity {
  id: string;
  name: string;
  type: string;
  description: string;
  tags: string;
  origin: string;
  metadata?: { profile?: Record<string, string>; suggested_relations?: string[] };
}

const form = reactive({
  name: "",
  concept: "",
  useReferences: false,
  ageGender: "",
  species: "",
  role: UNDECIDED,
  roleCustom: "",
  factionId: "",
  locationId: "",
  personality: "",
  voice: "",
  relations: [] as { target: string; note: string }[],
  notes: "",
});

const archiveEntities = ref<ArchiveEntity[]>([]);
const factions = computed(() => archiveEntities.value.filter((entity) => entity.type === "faction"));
const locations = computed(() =>
  archiveEntities.value.filter((entity) => entity.type === "location"),
);
const npcs = computed(() => archiveEntities.value.filter((entity) => entity.type === "npc"));
const archiveTotal = ref(0);
const archiveNextCursor = ref<string | null>(null);
const archiveLoading = ref(false);
const archiveError = ref("");
const ARCHIVE_PAGE_SIZE = 40;
let archiveLoadSequence = 0;

interface CharacterResult {
  name: string;
  summary: string;
  profile: Record<string, string>;
  relations: { target: string; kind: string }[];
  suggested: string[];
  inspirationRefs: string[];
  referenceReport: ReferenceReportRow[];
  referencesRequested: boolean;
  autoReviewIncomplete: boolean;
}

interface ReferenceReportRow {
  source_ref: string;
  source_title: string;
  used_for: string;
  transformation: string;
  leakage?: { checked?: boolean; detected?: boolean; message?: string } | null;
}

const job = getJobChannel<CharacterResult>("character_profile", STAGES);
const { running, stageIndex, elapsed, result } = toRefs(job);
const lastCost = toRef(job, "cost");
const llmReady = ref(llmConfig().ready);

const expanded = ref<string>("");
const editing = ref<string>("");
const editDraft = reactive<{ description: string; tags: string; profile: Record<string, string> }>(
  { description: "", tags: "", profile: {} },
);
const maintainFlash = ref("");
const showCard = ref(false);

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}

onUnmounted(() => {
  archiveLoadSequence += 1;
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});

const canRun = computed(
  () =>
    form.name.trim().length > 0 &&
    form.concept.trim().length > 0 &&
    !running.value &&
    llmReady.value,
);

interface ArchiveEntityPage {
  items: ArchiveEntity[];
  total: number;
  next_cursor: string | null;
}

async function loadArchive(cursor = "", append = false): Promise<void> {
  if (append && archiveLoading.value) return;
  const sequence = ++archiveLoadSequence;
  archiveLoading.value = true;
  if (!append) archiveError.value = "";
  try {
    const params = new URLSearchParams({ kind: "entity", limit: String(ARCHIVE_PAGE_SIZE) });
    if (cursor) params.set("cursor", cursor);
    const body = await apiGet<ArchiveEntityPage>(
      `/projects/${currentProject()}/archive?${params.toString()}`,
    );
    if (sequence !== archiveLoadSequence) return;
    const rows = append ? [...archiveEntities.value, ...body.items] : body.items;
    archiveEntities.value = [...new Map(rows.map((entity) => [entity.id, entity])).values()];
    archiveTotal.value = body.total;
    archiveNextCursor.value = body.next_cursor;
  } catch (e) {
    if (sequence !== archiveLoadSequence) return;
    if (cursor && e instanceof ApiError && e.status === 409) {
      // A canon edit invalidates the opaque snapshot cursor. Restarting avoids duplicate or
      // skipped characters while still leaving the page usable without a manual refresh.
      await loadArchive();
      return;
    }
    archiveError.value = humanizeError(e);
  } finally {
    if (sequence === archiveLoadSequence) archiveLoading.value = false;
  }
}

async function loadMoreArchive(): Promise<void> {
  if (!archiveNextCursor.value) return;
  await loadArchive(archiveNextCursor.value, true);
}

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  try {
    await loadArchive();
  } catch (e) {
    notifyError(e);
  }
  void resumeJob<CharacterResult>("character_profile", {
    stages: STAGES,
    onEvent: onCharacterEvent,
    parseResult: parseCharacterResult,
  });
});

function addRelationRow(): void {
  form.relations.push({ target: "", note: "" });
}

function removeRelationRow(index: number): void {
  form.relations.splice(index, 1);
}

function onCharacterEvent(
  ch: { stageIndex: number },
  event: { type: string; data: Record<string, unknown> },
): void {
  if (event.type !== "stage") return;
  const index = STAGES.findIndex((stage) => stage.key === String(event.data.name ?? ""));
  if (index > ch.stageIndex) ch.stageIndex = index;
}

function parseCharacterResult(raw: Record<string, unknown>): CharacterResult {
  const source = raw as {
    entity?: { name: string; description: string };
    profile?: Record<string, string>;
    relations?: { target: string; kind: string }[];
    suggested_relations?: string[];
    inspiration_refs?: string[];
    reference_report?: ReferenceReportRow[];
    auto_review_incomplete?: boolean;
  };
  const names = new Map(
    [...npcs.value, ...factions.value, ...locations.value].map((entity) => [entity.id, entity.name]),
  );
  return {
    name: source.entity?.name ?? form.name.trim(),
    summary: source.entity?.description ?? "",
    profile: source.profile ?? {},
    relations: (source.relations ?? []).map((relation) => ({
      target: names.get(relation.target) ?? relation.target,
      kind: relation.kind,
    })),
    suggested: source.suggested_relations ?? [],
    inspirationRefs: source.inspiration_refs ?? [],
    referenceReport: source.reference_report ?? [],
    referencesRequested:
      Object.prototype.hasOwnProperty.call(raw, "inspiration_refs") ||
      Object.prototype.hasOwnProperty.call(raw, "reference_report"),
    autoReviewIncomplete: source.auto_review_incomplete ?? false,
  };
}

async function run(): Promise<void> {
  if (!canRun.value) return;
  const role = form.role === UNDECIDED ? "" : form.role === CUSTOM ? form.roleCustom.trim() : form.role;
  const nameOf = new Map(npcs.value.map((n) => [n.id, n.name]));
  await startJob<CharacterResult>("character_profile", {
    kind: "character_profile",
    stages: STAGES,
    params: {
      ...llmParams(),
      brief: {
        name: form.name.trim(),
        concept: form.concept.trim(),
        use_references: form.useReferences,
        age_gender: form.ageGender.trim(),
        species: form.species.trim(),
        role_function: role,
        faction_id: form.factionId,
        location_id: form.locationId,
        personality_hints: form.personality.trim(),
        voice_hints: form.voice.trim(),
        relationship_hints: form.relations
          .filter((r) => r.target && r.note.trim())
          .map((r) => `与 ${r.target}（${nameOf.get(r.target) ?? r.target}）：${r.note.trim()}`),
        notes: form.notes.trim(),
      },
    },
    onEvent: onCharacterEvent,
    parseResult: parseCharacterResult,
  });
}

function startEdit(npc: ArchiveEntity): void {
  editing.value = npc.id;
  editDraft.description = npc.description;
  editDraft.tags = npc.tags;
  editDraft.profile = { ...(npc.metadata?.profile ?? {}) };
}

async function saveEdit(npc: ArchiveEntity): Promise<void> {
  maintainFlash.value = "";
  try {
    await apiPatch(`/projects/${currentProject()}/entities/${encodeURIComponent(npc.id)}`, {
      description: editDraft.description,
      tags: editDraft.tags
        .split(/[,，]/)
        .map((t) => t.trim())
        .filter(Boolean),
      metadata_updates: { profile: editDraft.profile },
    });
    editing.value = "";
    maintainFlash.value = `已更新 ${npc.name}。`;
    await loadArchive();
  } catch (e) {
    maintainFlash.value = humanizeError(e);
  }
}

async function removeNpc(npc: ArchiveEntity): Promise<void> {
  if (
    !(await askConfirm(`删除「${npc.name}」？其关系会一并移除。`, {
      title: "删除角色",
      confirmLabel: "删除角色",
    }))
  )
    return;
  maintainFlash.value = "";
  try {
    await apiDelete(`/projects/${currentProject()}/objects/entity/${encodeURIComponent(npc.id)}`);
    maintainFlash.value = `已删除 ${npc.name}。`;
    await loadArchive();
  } catch (e) {
    maintainFlash.value = humanizeError(e);
  }
}
</script>

<template>
  <section>
    <PageHead overline="CHARACTERS" title="人物工坊" purpose="生成可维护的角色卡，名字与概念必填。" />

    <div class="pane form">
      <div class="grid">
        <label class="field">
          <span class="label">名字 <em>必填</em></span>
          <input v-model="form.name" maxlength="80" placeholder="例如：白盐" />
        </label>
        <label class="field">
          <span class="label">戏剧定位</span>
          <select v-model="form.role">
            <option :value="UNDECIDED">{{ UNDECIDED }}</option>
            <option v-for="option in ROLE_OPTIONS" :key="option" :value="option">
              {{ option }}
            </option>
            <option :value="CUSTOM">{{ CUSTOM }}</option>
          </select>
          <input v-if="form.role === CUSTOM" v-model="form.roleCustom" placeholder="自由填写" />
        </label>
      </div>
      <label class="field">
        <span class="label">
          一句话概念 <em>必填</em>
          <i class="muted">{{ form.concept.length }}/2000</i>
        </span>
        <textarea
          v-model="form.concept"
          rows="2"
          maxlength="2000"
          :placeholder="`例如：${phConcept}`"
        ></textarea>
      </label>
      <label class="ground-row">
        <input v-model="form.useReferences" type="checkbox" />
        <span>借鉴灵感库 <i class="muted">只取相关片段作灵感，引用会随待审角色卡保留</i></span>
      </label>
      <div class="grid">
        <label class="field">
          <span class="label">年龄 / 性别</span>
          <input v-model="form.ageGender" placeholder="可留空" />
        </label>
        <label class="field">
          <span class="label">种族 / 族裔</span>
          <input v-model="form.species" placeholder="可留空" />
        </label>
        <label class="field">
          <span class="label">所属阵营</span>
          <select v-model="form.factionId">
            <option value="">暂不指定</option>
            <option v-for="f in factions" :key="f.id" :value="f.id">{{ f.name }}</option>
          </select>
        </label>
        <label class="field">
          <span class="label">常驻地点</span>
          <select v-model="form.locationId">
            <option value="">暂不指定</option>
            <option v-for="l in locations" :key="l.id" :value="l.id">{{ l.name }}</option>
          </select>
        </label>
        <label class="field">
          <span class="label">性格倾向</span>
          <input v-model="form.personality" placeholder="例如：外冷内热、嘴硬" />
        </label>
        <label class="field">
          <span class="label">说话方式</span>
          <input v-model="form.voice" placeholder="例如：短句，带海腔" />
        </label>
      </div>

      <div class="field">
        <span class="label">与既有角色的关系</span>
        <div v-for="(row, index) in form.relations" :key="index" class="rel-row">
          <select v-model="row.target">
            <option value="">选择对象…</option>
            <option v-for="n in npcs" :key="n.id" :value="n.id">{{ n.name }}</option>
          </select>
          <input v-model="row.note" placeholder="什么关系，例如：互相欠过人情的旧识" />
          <button type="button" class="ghost" @click="removeRelationRow(index)">移除</button>
        </div>
        <button type="button" class="ghost add" @click="addRelationRow">+ 添加一条关系</button>
      </div>

      <label class="field">
        <span class="label">补充要求</span>
        <input v-model="form.notes" placeholder="可留空" />
      </label>

      <button class="primary" :disabled="!canRun" @click="run">
        {{ running ? "正在落笔…" : "生成角色卡" }}
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
    />

    <!-- concise result card: name + one-line summary + key facets; full sheet opens on demand -->
    <div v-if="result" class="pane done reveal">
      <h3 class="cast-name">
        {{ result.name }}
        <span v-if="lastCost" class="cost">本次 ${{ lastCost.toFixed(4) }}</span>
      </h3>
      <p class="summary">{{ result.summary }}</p>
      <div class="done-foot">
        <button class="card-open" @click="showCard = true">查看完整角色卡</button>
        <span class="muted small">
          {{ Object.keys(result.profile).length }} 项设定 · {{ result.relations.length }} 条关系 ·
          灵感引用 {{ result.inspirationRefs.length }} 条 · 已入审阅台候批
        </span>
      </div>
      <p v-if="result.autoReviewIncomplete" class="warn">
        自评环节没能给出可信结论，这份角色卡未经自动校验，请在审阅时多看一眼。
      </p>
    </div>

    <Modal :open="showCard" overline="CHARACTER" :title="result?.name ?? ''" @close="showCard = false">
      <div v-if="result" class="cardview reveal">
        <p class="summary lead">{{ result.summary }}</p>
        <div v-for="(text, key) in result.profile" :key="key" class="profile-row">
          <span class="profile-label">{{ PROFILE_LABELS[key] ?? key }}</span>
          <span>{{ text }}</span>
        </div>
        <template v-if="result.relations.length">
          <div class="section sub"><span class="t">关系</span></div>
          <div class="chips">
            <span v-for="(rel, index) in result.relations" :key="index" class="chip static">
              {{ result.name }} <i>{{ rel.kind }}</i> {{ rel.target }}
            </span>
          </div>
        </template>
        <p v-if="result.suggested.length" class="muted small">
          模型还提到了档案外的关联（未入图，仅供参考）：{{ result.suggested.join("；") }}
        </p>
        <details v-if="result.referencesRequested" class="reference-report">
          <summary>灵感溯源 · {{ result.inspirationRefs.length }} 条引用</summary>
          <p v-if="!result.referenceReport.length" class="muted small">本次没有召回可用于草拟的素材。</p>
          <ul v-else>
            <li v-for="row in result.referenceReport" :key="row.source_ref">
              <b>{{ row.source_title }}</b> · {{ row.used_for }}
              <span class="muted">{{ row.transformation }}</span>
              <span v-if="row.leakage?.message" :class="{ danger: row.leakage.detected }">
                {{ row.leakage.message }}
              </span>
            </li>
          </ul>
        </details>
      </div>
    </Modal>

    <div class="section maintain-head"><span class="t">已有角色</span></div>
    <p v-if="archiveEntities.length" class="muted archive-status">
      档案实体已加载 {{ archiveEntities.length }} / {{ archiveTotal }} 项，其中角色
      {{ npcs.length }} 项。
    </p>
    <p v-if="maintainFlash" class="flash">{{ maintainFlash }}</p>
    <p v-if="archiveError" class="error">读取角色档案失败：{{ archiveError }}</p>
    <p v-else-if="archiveLoading && !archiveEntities.length" class="muted">正在载入角色档案…</p>
    <p v-else-if="!npcs.length" class="muted">
      {{ archiveNextCursor ? "当前已加载范围内没有角色，可继续加载。" : "还没有角色。" }}
    </p>
    <div v-for="npc in npcs" :key="npc.id" class="pane card">
      <div
        class="head"
        role="button"
        tabindex="0"
        :aria-expanded="expanded === npc.id"
        @click="expanded = expanded === npc.id ? '' : npc.id"
        @keydown.enter="expanded = expanded === npc.id ? '' : npc.id"
        @keydown.space.prevent="expanded = expanded === npc.id ? '' : npc.id"
      >
        <b>{{ npc.name }}</b>
        <span class="mono">{{ npc.id }}</span>
        <span class="muted desc">{{ npc.description }}</span>
      </div>
      <div v-if="expanded === npc.id" class="body">
        <template v-if="editing !== npc.id">
          <div
            v-for="(text, key) in npc.metadata?.profile ?? {}"
            :key="key"
            class="profile-row"
          >
            <span class="profile-label">{{ PROFILE_LABELS[key] ?? key }}</span>
            <span>{{ text }}</span>
          </div>
          <div class="actions">
            <button @click="startEdit(npc)">编辑</button>
            <button class="ghost" @click="removeNpc(npc)">删除</button>
          </div>
        </template>
        <template v-else>
          <label class="field">
            <span class="label">简介</span>
            <textarea v-model="editDraft.description" rows="2"></textarea>
          </label>
          <label class="field">
            <span class="label">标签（逗号分隔）</span>
            <input v-model="editDraft.tags" />
          </label>
          <label v-for="(label, key) in PROFILE_LABELS" :key="key" class="field">
            <span class="label">{{ label }}</span>
            <textarea v-model="editDraft.profile[key]" rows="2"></textarea>
          </label>
          <div class="actions">
            <button class="primary" @click="saveEdit(npc)">保存</button>
            <button class="ghost" @click="editing = ''">取消</button>
          </div>
        </template>
      </div>
    </div>
    <div v-if="archiveNextCursor" class="pagination-row" aria-label="角色档案分页">
      <span class="muted small">继续载入也会补全阵营、地点和关系选择器。</span>
      <button :disabled="archiveLoading" @click="loadMoreArchive">
        {{ archiveLoading ? "载入中…" : "加载下一页" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
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

.ground-row {
  display: flex;
  align-items: center;
  gap: 0.5rem;
  color: var(--ow-ink);
  font-size: 0.84rem;
  cursor: pointer;
}

.ground-row input {
  width: auto;
  margin: 0;
}

.ground-row i {
  font-style: normal;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
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
  margin-left: 0.4rem;
}

textarea,
input,
select {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: var(--ow-control-radius);
  color: var(--ow-ink);
  padding: 0.5rem 0.65rem;
  font: inherit;
  font-size: 0.88rem;
}

select:focus,
input:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 0.8rem;
}

.rel-row {
  display: grid;
  grid-template-columns: 11rem 1fr auto;
  gap: 0.5rem;
  margin-bottom: 0.45rem;
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

button.ghost {
  color: var(--ow-muted);
}

button.ghost:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-ink);
}

button.add {
  align-self: flex-start;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border-color: rgba(240, 210, 138, 0.65);
  color: #241a05;
  font-weight: 600;
  box-shadow: 0 0 12px rgba(217, 181, 108, 0.2);
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

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  color: var(--ow-cyan);
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  padding: 0.12rem 0.5rem;
  margin-left: 0.5rem;
  vertical-align: middle;
}

.error {
  color: #e89a9a;
}

.flash {
  color: #8ed4ac;
}

.done {
  margin-top: 0.9rem;
  padding: 0.95rem 1.15rem;
}

.cast-name {
  margin: 0 0 0.2rem;
  color: var(--ow-gold-bright);
}

.summary {
  margin: 0 0 0.6rem;
}
.summary.lead {
  font-size: 0.95rem;
  line-height: 1.75;
}

.done-foot {
  display: flex;
  align-items: center;
  gap: 0.7rem;
  flex-wrap: wrap;
}
.card-open {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
  border-radius: var(--ow-control-radius);
  padding: 0.4rem 0.85rem;
  font: inherit;
  font-size: 0.84rem;
  cursor: pointer;
  transition: box-shadow 0.15s ease;
}
.card-open:hover {
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.22);
}
.cardview {
  display: flex;
  flex-direction: column;
  gap: 0.55rem;
}

.reference-report {
  margin-top: 0.45rem;
  padding: 0.55rem 0.65rem;
  border: 1px solid var(--ow-line);
  border-radius: 8px;
  background: var(--ow-panel-2);
  font-size: 0.78rem;
}

.reference-report summary {
  color: var(--ow-gold-bright);
  cursor: pointer;
}

.reference-report ul {
  margin: 0.5rem 0 0;
  padding-left: 1.1rem;
}

.reference-report li,
.reference-report li span {
  display: block;
  margin-top: 0.2rem;
}

.reference-report .danger {
  color: #e89a9a;
}

.profile-row {
  display: grid;
  grid-template-columns: 6.5rem 1fr;
  gap: 0.6rem;
  padding: 0.3rem 0;
  font-size: 0.87rem;
  border-bottom: 1px solid rgba(46, 54, 88, 0.45);
}

.profile-label {
  color: var(--ow-muted);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.6rem 0 0.3rem;
}

.chip.static {
  border: 1px solid var(--ow-line);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  background: rgba(16, 22, 48, 0.6);
  font-size: 0.78rem;
  padding: 0.18rem 0.65rem;
}

.chip.static i {
  color: var(--ow-cyan);
  font-style: normal;
  margin: 0 0.3rem;
}

.small {
  font-size: 0.78rem;
}

.maintain-head {
  margin-top: 1.4rem;
}

.archive-status {
  margin: -0.2rem 0 0.65rem;
  font-size: 0.78rem;
}

.pagination-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
  margin-top: 0.7rem;
}

.pagination-row button:disabled {
  cursor: wait;
  opacity: 0.6;
}

.card {
  padding: 0.7rem 1rem;
  margin-bottom: 0.6rem;
}

.card .head {
  display: flex;
  gap: 0.7rem;
  align-items: baseline;
  cursor: pointer;
}

.card .head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.mono {
  font-family: ui-monospace, Consolas, monospace;
  color: var(--ow-cyan);
  font-size: 0.76rem;
}

.desc {
  font-size: 0.82rem;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.card .body {
  margin-top: 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.actions {
  display: flex;
  gap: 0.5rem;
  margin-top: 0.4rem;
}

@media (max-width: 600px) {
  .grid { grid-template-columns: 1fr; }
  .rel-row { grid-template-columns: 1fr; }
  .rel-row > * { min-width: 0; width: 100%; }
  .profile-row { grid-template-columns: 1fr; gap: 0.2rem; }
  .card .head { align-items: flex-start; flex-direction: column; gap: 0.25rem; }
  .desc { width: 100%; white-space: normal; }
  .actions { flex-direction: column; }
  .actions button { width: 100%; min-height: 44px; }
  .pagination-row { align-items: stretch; flex-direction: column; }
}
</style>
