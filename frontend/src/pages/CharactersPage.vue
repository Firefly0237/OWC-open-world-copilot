<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import StepperProgress from "../components/StepperProgress.vue";
import {
  addSessionCost,
  apiDelete,
  apiGet,
  apiPatch,
  apiPost,
  costOf,
  currentProject,
  llmConfig,
  llmParams,
  streamJobEvents,
} from "../api";

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

const factions = ref<ArchiveEntity[]>([]);
const locations = ref<ArchiveEntity[]>([]);
const npcs = ref<ArchiveEntity[]>([]);

const running = ref(false);
const stageIndex = ref(-1);
const elapsed = ref(0);
const error = ref("");
const lastCost = ref(0);
const llmReady = ref(llmConfig().ready);
const result = ref<{
  name: string;
  summary: string;
  profile: Record<string, string>;
  relations: { target: string; kind: string }[];
  suggested: string[];
} | null>(null);

const expanded = ref<string>("");
const editing = ref<string>("");
const editDraft = reactive<{ description: string; tags: string; profile: Record<string, string> }>(
  { description: "", tags: "", profile: {} },
);
const maintainFlash = ref("");

let timer: number | undefined;

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}

onUnmounted(() => {
  if (timer !== undefined) window.clearInterval(timer);
  window.removeEventListener("ow-llm-changed", onLlmChanged);
});

const canRun = computed(
  () =>
    form.name.trim().length > 0 &&
    form.concept.trim().length > 0 &&
    !running.value &&
    llmReady.value,
);

async function loadArchive(): Promise<void> {
  const body = await apiGet<{ inventory: { entities: ArchiveEntity[] } }>(
    `/projects/${currentProject()}/archive`,
  );
  const all = body.inventory.entities;
  factions.value = all.filter((e) => e.type === "faction");
  locations.value = all.filter((e) => e.type === "location");
  npcs.value = all.filter((e) => e.type === "npc");
}

onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  try {
    await loadArchive();
  } catch (e) {
    error.value = String(e);
  }
});

function addRelationRow(): void {
  form.relations.push({ target: "", note: "" });
}

function removeRelationRow(index: number): void {
  form.relations.splice(index, 1);
}

async function run(): Promise<void> {
  if (!canRun.value) return;
  running.value = true;
  stageIndex.value = 0;
  elapsed.value = 0;
  result.value = null;
  error.value = "";
  if (timer !== undefined) window.clearInterval(timer);
  timer = window.setInterval(() => {
    elapsed.value += 1;
  }, 1000);
  const role = form.role === UNDECIDED ? "" : form.role === CUSTOM ? form.roleCustom.trim() : form.role;
  const nameOf = new Map(npcs.value.map((n) => [n.id, n.name]));
  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${currentProject()}/jobs`, {
      kind: "character_profile",
      params: {
        ...llmParams(),
        brief: {
          name: form.name.trim(),
          concept: form.concept.trim(),
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
    });
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "stage") {
        const index = STAGES.findIndex((s) => s.key === String(event.data.name ?? ""));
        if (index > stageIndex.value) stageIndex.value = index;
      } else if (event.type === "failed") {
        error.value = String(event.data.error ?? "任务失败");
      }
    });
    const status = await apiGet<{
      status: string;
      result: {
        entity?: { name: string; description: string };
        profile?: Record<string, string>;
        relations?: { target: string; kind: string }[];
        suggested_relations?: string[];
      } | null;
      error: string | null;
    }>(`/jobs/${job.job_id}`);
    if (status.status === "done" && status.result) {
      stageIndex.value = STAGES.length - 1;
      const used = costOf(status.result as { cost_budget?: { used_usd?: number } });
      lastCost.value = used;
      addSessionCost(used);
      const allNames = new Map(
        [...npcs.value, ...factions.value, ...locations.value].map((e) => [e.id, e.name]),
      );
      result.value = {
        name: status.result.entity?.name ?? form.name,
        summary: status.result.entity?.description ?? "",
        profile: status.result.profile ?? {},
        relations: (status.result.relations ?? []).map((r) => ({
          target: allNames.get(r.target) ?? r.target,
          kind: r.kind,
        })),
        suggested: status.result.suggested_relations ?? [],
      };
    } else if (!error.value) {
      error.value = status.error ?? "任务未完成";
    }
  } catch (e) {
    error.value = String(e);
  } finally {
    running.value = false;
    if (timer !== undefined) window.clearInterval(timer);
  }
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
    maintainFlash.value = String(e);
  }
}

async function removeNpc(npc: ArchiveEntity): Promise<void> {
  if (!window.confirm(`删除「${npc.name}」？其关系会一并移除。`)) return;
  maintainFlash.value = "";
  try {
    await apiDelete(`/projects/${currentProject()}/objects/entity/${encodeURIComponent(npc.id)}`);
    maintainFlash.value = `已删除 ${npc.name}。`;
    await loadArchive();
  } catch (e) {
    maintainFlash.value = String(e);
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">人物工坊 · 角色卡</span></div>
    <p class="muted hint">名字和一句话概念必填，其余想到哪选到哪。生成的角色会进审阅台候批。</p>

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
          placeholder="例如：走私船上长大、如今执法的年轻巡查官，正在查一桩牵连养父的旧案。"
        ></textarea>
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
        生成走真实模型——先在
        <RouterLink to="/settings" class="golink">设置</RouterLink>
        接入服务商与 API Key。
      </p>
    </div>

    <StepperProgress
      v-if="running || stageIndex >= 0"
      :stages="STAGES"
      :index="stageIndex"
      :running="running"
      :elapsed="elapsed"
    />

    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="result" class="pane done">
      <h3 class="cast-name">
        {{ result.name }}
        <span v-if="lastCost" class="cost">本次 ${{ lastCost.toFixed(4) }}</span>
      </h3>
      <p class="summary">{{ result.summary }}</p>
      <div v-for="(text, key) in result.profile" :key="key" class="profile-row">
        <span class="profile-label">{{ PROFILE_LABELS[key] ?? key }}</span>
        <span>{{ text }}</span>
      </div>
      <div v-if="result.relations.length" class="chips">
        <span v-for="(rel, index) in result.relations" :key="index" class="chip static">
          {{ result.name }} <i>{{ rel.kind }}</i> {{ rel.target }}
        </span>
      </div>
      <p v-if="result.suggested.length" class="muted small">
        模型还提到了档案外的关联（未入图，仅供参考）：{{ result.suggested.join("；") }}
      </p>
      <p class="muted">已入审阅台，采纳后正式入档。</p>
    </div>

    <div class="section maintain-head"><span class="t">已有角色</span></div>
    <p v-if="maintainFlash" class="flash">{{ maintainFlash }}</p>
    <p v-if="!npcs.length" class="muted">还没有角色。</p>
    <div v-for="npc in npcs" :key="npc.id" class="pane card">
      <div class="head" @click="expanded = expanded === npc.id ? '' : npc.id">
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
  border-radius: 0.5rem;
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
  border-radius: 0.5rem;
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
  border-radius: 999px;
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
  border-radius: 999px;
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
</style>
