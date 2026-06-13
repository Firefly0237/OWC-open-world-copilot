<script setup lang="ts">
import { computed, onMounted, onUnmounted, reactive, ref } from "vue";
import {
  addSessionCost,
  apiGet,
  apiPost,
  costOf,
  currentProject,
  llmConfig,
  llmParams,
} from "../api";

type Tab = "draft" | "dialogue" | "barks" | "flavor";
const TABS: { key: Tab; label: string }[] = [
  { key: "draft", label: "任务草稿" },
  { key: "dialogue", label: "对话树" },
  { key: "barks", label: "台词" },
  { key: "flavor", label: "物案" },
];

interface ArchiveEntity {
  id: string;
  name: string;
  type: string;
}

const tab = ref<Tab>("draft");
const npcs = ref<ArchiveEntity[]>([]);
const error = ref("");
const llmReady = ref(llmConfig().ready);

// shared per-tab busy + result note + cost
const busy = reactive<Record<Tab, boolean>>({ draft: false, dialogue: false, barks: false, flavor: false });
const lastCost = reactive<Record<Tab, number>>({ draft: 0, dialogue: 0, barks: 0, flavor: 0 });

// draft
const draftBrief = ref("");
const draftResult = ref<{ title: string; objective: string; stages: string[]; issues: number } | null>(null);

// dialogue
const dlgParticipants = ref<string[]>([]);
const dlgBrief = ref("");
const dlgResult = ref<{ nodes: number; lint: number; problems: number } | null>(null);

// barks
const barkSpeakers = ref<string[]>([]);
const barkTopic = ref("");
const barkVariants = ref(3);
const barkResult = ref<{ accepted: { speaker_id: string; text: string }[]; rejected: number } | null>(null);

// flavor
const flavorCategory = ref<"item" | "skill" | "achievement">("item");
const flavorNames = ref("");
const flavorTheme = ref("");
const flavorResult = ref<{ accepted: { name: string; description?: string }[]; rejected: number } | null>(null);

const FLAVOR_LABEL = { item: "物品", skill: "技能", achievement: "成就" };

function onLlmChanged(): void {
  llmReady.value = llmConfig().ready;
}
onMounted(async () => {
  window.addEventListener("ow-llm-changed", onLlmChanged);
  try {
    const body = await apiGet<{ inventory: { entities: ArchiveEntity[] } }>(
      `/projects/${currentProject()}/archive`,
    );
    npcs.value = body.inventory.entities.filter((e) => e.type === "npc");
  } catch (e) {
    error.value = String(e);
  }
});
onUnmounted(() => window.removeEventListener("ow-llm-changed", onLlmChanged));

function nameOf(id: string): string {
  return npcs.value.find((n) => n.id === id)?.name ?? id;
}

function toggle(list: { value: string[] }, id: string): void {
  const i = list.value.indexOf(id);
  if (i >= 0) list.value.splice(i, 1);
  else list.value.push(id);
}

async function call<T>(key: Tab, path: string, body: unknown): Promise<T | null> {
  busy[key] = true;
  error.value = "";
  try {
    const res = await apiPost<T & { cost_budget?: { used_usd?: number } }>(path, body);
    const used = costOf(res);
    lastCost[key] = used;
    addSessionCost(used);
    return res;
  } catch (e) {
    error.value = String(e);
    return null;
  } finally {
    busy[key] = false;
  }
}

async function runDraft(): Promise<void> {
  if (!draftBrief.value.trim() || !llmReady.value) return;
  draftResult.value = null;
  const r = await call<{ quest: { title: string; objective: string; stages?: { summary: string }[] }; issues: unknown[] }>(
    "draft",
    `/projects/${currentProject()}/contents/quests:draft`,
    { brief: draftBrief.value.trim(), ...llmParams() },
  );
  if (r)
    draftResult.value = {
      title: r.quest.title,
      objective: r.quest.objective,
      stages: (r.quest.stages ?? []).map((s) => s.summary),
      issues: r.issues.length,
    };
}

async function runDialogue(): Promise<void> {
  if (dlgParticipants.value.length === 0 || !dlgBrief.value.trim() || !llmReady.value) return;
  dlgResult.value = null;
  const r = await call<{ tree: { nodes?: unknown[] }; lint_issues: unknown[]; structure_problems: unknown[] }>(
    "dialogue",
    `/projects/${currentProject()}/assist/dialogue_trees:draft`,
    { participant_ids: dlgParticipants.value, brief: dlgBrief.value.trim(), ...llmParams() },
  );
  if (r)
    dlgResult.value = {
      nodes: (r.tree.nodes ?? []).length,
      lint: r.lint_issues.length,
      problems: r.structure_problems.length,
    };
}

async function runBarks(): Promise<void> {
  if (barkSpeakers.value.length === 0 || !barkTopic.value.trim() || !llmReady.value) return;
  barkResult.value = null;
  const r = await call<{ accepted: { speaker_id: string; text: string }[]; rejected: unknown[] }>(
    "barks",
    `/projects/${currentProject()}/assist/barks:batch`,
    {
      speaker_ids: barkSpeakers.value,
      topic: barkTopic.value.trim(),
      variants_per_speaker: barkVariants.value,
      ...llmParams(),
    },
  );
  if (r) barkResult.value = { accepted: r.accepted, rejected: r.rejected.length };
}

async function runFlavor(): Promise<void> {
  const names = flavorNames.value
    .split(/[\n,，]/)
    .map((n) => n.trim())
    .filter(Boolean);
  if (names.length === 0 || !llmReady.value) return;
  flavorResult.value = null;
  const r = await call<{ accepted: { name: string; description?: string }[]; rejected: unknown[] }>(
    "flavor",
    `/projects/${currentProject()}/assist/flavor:batch`,
    { category: flavorCategory.value, names, theme: flavorTheme.value.trim(), ...llmParams() },
  );
  if (r) flavorResult.value = { accepted: r.accepted, rejected: r.rejected.length };
}

const flavorNameCount = computed(
  () => flavorNames.value.split(/[\n,，]/).filter((n) => n.trim()).length,
);
</script>

<template>
  <section>
    <div class="section"><span class="t">创作工坊</span></div>
    <p class="muted hint">受约束生成：只引用图谱内的实体、生成即审计/lint，全部进审阅台候批——人审采纳是落盘的唯一通道。</p>

    <div class="tabs">
      <button v-for="t in TABS" :key="t.key" class="tab" :class="{ on: tab === t.key }" @click="tab = t.key">
        {{ t.label }}
      </button>
    </div>

    <p v-if="!llmReady" class="muted notice">
      创作走真实模型——先在 <RouterLink to="/settings" class="golink">设置</RouterLink> 接入服务商与 API Key。
    </p>
    <p v-if="error" class="error">{{ error }}</p>

    <!-- 任务草稿 -->
    <div v-show="tab === 'draft'" class="pane form">
      <label class="field">
        <span class="label">任务简述</span>
        <textarea v-model="draftBrief" rows="3" maxlength="4000" placeholder="例如：护送一车雾铃零件穿过寂静湾，途中要决定是否相信一名声称能听懂错音的领航员。"></textarea>
      </label>
      <button class="primary" :disabled="busy.draft || !draftBrief.trim() || !llmReady" @click="runDraft">
        {{ busy.draft ? "起草中…" : "生成任务草稿" }}
      </button>
      <div v-if="draftResult" class="result">
        <div class="r-head"><b>{{ draftResult.title }}</b><span v-if="lastCost.draft" class="cost">${{ lastCost.draft.toFixed(4) }}</span></div>
        <p class="objective">{{ draftResult.objective }}</p>
        <ol v-if="draftResult.stages.length" class="stages">
          <li v-for="(s, i) in draftResult.stages" :key="i">{{ s }}</li>
        </ol>
        <p class="muted small">生成时自检出 {{ draftResult.issues }} 处待留意；草案已入审阅台候批。</p>
      </div>
    </div>

    <!-- 对话树 -->
    <div v-show="tab === 'dialogue'" class="pane form">
      <div class="field">
        <span class="label">参与角色（点选，至少 1 个）</span>
        <div class="chips">
          <button v-for="n in npcs" :key="n.id" type="button" class="chip" :class="{ on: dlgParticipants.includes(n.id) }" @click="toggle(dlgParticipants, n.id)">{{ n.name }}</button>
          <span v-if="!npcs.length" class="muted small">这个世界还没有角色，先去人物工坊或创世生成。</span>
        </div>
      </div>
      <label class="field">
        <span class="label">情境简述</span>
        <textarea v-model="dlgBrief" rows="2" maxlength="2000" placeholder="例如：玩家追问老桅上个月听到的错音，他欲言又止。"></textarea>
      </label>
      <button class="primary" :disabled="busy.dialogue || !dlgParticipants.length || !dlgBrief.trim() || !llmReady" @click="runDialogue">
        {{ busy.dialogue ? "推演中…" : "生成对话树" }}
      </button>
      <div v-if="dlgResult" class="result">
        <div class="r-head"><b>对话树已生成</b><span v-if="lastCost.dialogue" class="cost">${{ lastCost.dialogue.toFixed(4) }}</span></div>
        <div class="chips static">
          <span class="chip">{{ dlgResult.nodes }} 节点</span>
          <span class="chip" :class="{ amber: dlgResult.lint }">{{ dlgResult.lint }} lint</span>
          <span class="chip" :class="{ red: dlgResult.problems }">{{ dlgResult.problems }} 结构问题</span>
        </div>
        <p class="muted small">已入审阅台候批。</p>
      </div>
    </div>

    <!-- 台词 -->
    <div v-show="tab === 'barks'" class="pane form">
      <div class="field">
        <span class="label">说话人（点选，至少 1 个）</span>
        <div class="chips">
          <button v-for="n in npcs" :key="n.id" type="button" class="chip" :class="{ on: barkSpeakers.includes(n.id) }" @click="toggle(barkSpeakers, n.id)">{{ n.name }}</button>
          <span v-if="!npcs.length" class="muted small">这个世界还没有角色。</span>
        </div>
      </div>
      <label class="field">
        <span class="label">话题 / 场合</span>
        <input v-model="barkTopic" maxlength="1000" placeholder="例如：发现可疑船影时的警戒喊话" />
      </label>
      <label class="field inline">
        <span class="label">每人变体数 {{ barkVariants }}</span>
        <input v-model.number="barkVariants" type="range" min="1" max="8" />
      </label>
      <button class="primary" :disabled="busy.barks || !barkSpeakers.length || !barkTopic.trim() || !llmReady" @click="runBarks">
        {{ busy.barks ? "生成中…" : "生成台词" }}
      </button>
      <div v-if="barkResult" class="result">
        <div class="r-head"><b>采纳 {{ barkResult.accepted.length }} 条</b><span v-if="lastCost.barks" class="cost">${{ lastCost.barks.toFixed(4) }}</span></div>
        <div class="lines">
          <div v-for="(v, i) in barkResult.accepted" :key="i" class="line"><span class="who">{{ nameOf(v.speaker_id) }}</span>{{ v.text }}</div>
        </div>
        <p class="muted small">{{ barkResult.rejected ? `${barkResult.rejected} 条因引用越界被拦下。` : "" }}已入审阅台候批。</p>
      </div>
    </div>

    <!-- 物案 -->
    <div v-show="tab === 'flavor'" class="pane form">
      <div class="field inline">
        <span class="label">类别</span>
        <select v-model="flavorCategory">
          <option value="item">物品</option>
          <option value="skill">技能</option>
          <option value="achievement">成就</option>
        </select>
      </div>
      <label class="field">
        <span class="label">名称（每行或逗号一个，最多 50 个）<i class="muted">{{ flavorNameCount }} 个</i></span>
        <textarea v-model="flavorNames" rows="3" placeholder="雾铃灯&#10;盐渍罗盘&#10;回音封蜡"></textarea>
      </label>
      <label class="field">
        <span class="label">风格基调（可选）</span>
        <input v-model="flavorTheme" maxlength="200" placeholder="例如：潮湿、年久、带咸味的工艺感" />
      </label>
      <button class="primary" :disabled="busy.flavor || !flavorNameCount || !llmReady" @click="runFlavor">
        {{ busy.flavor ? "撰写中…" : `生成${FLAVOR_LABEL[flavorCategory]}文案` }}
      </button>
      <div v-if="flavorResult" class="result">
        <div class="r-head"><b>采纳 {{ flavorResult.accepted.length }} 条</b><span v-if="lastCost.flavor" class="cost">${{ lastCost.flavor.toFixed(4) }}</span></div>
        <div class="cards">
          <div v-for="(e, i) in flavorResult.accepted" :key="i" class="fcard"><b>{{ e.name }}</b><span class="muted">{{ e.description }}</span></div>
        </div>
        <p class="muted small">{{ flavorResult.rejected ? `${flavorResult.rejected} 条被 lint 拦下。` : "" }}已入审阅台候批。</p>
      </div>
    </div>
  </section>
</template>

<style scoped>
.hint {
  font-size: 0.85rem;
}

.tabs {
  display: flex;
  gap: 0.4rem;
  margin-bottom: 0.8rem;
  flex-wrap: wrap;
}

.tab {
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem 0.5rem 0 0;
  background: transparent;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.86rem;
  padding: 0.4rem 0.9rem;
  cursor: pointer;
  border-bottom: 2px solid transparent;
}

.tab.on {
  color: var(--ow-gold-bright);
  border-bottom-color: var(--ow-gold);
}

.notice,
.error {
  font-size: 0.85rem;
}

.notice {
  color: var(--ow-muted);
}

.error {
  color: #e89a9a;
}

.golink {
  color: var(--ow-gold-bright);
  text-decoration: underline;
  text-underline-offset: 3px;
}

.form {
  padding: 1.1rem 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

.field {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
}

.field.inline {
  flex-direction: row;
  align-items: center;
  gap: 0.7rem;
}

.label {
  font-size: 0.82rem;
  color: var(--ow-muted);
}

.label i {
  font-style: normal;
  margin-left: 0.4rem;
  font-size: 0.74rem;
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

textarea {
  resize: vertical;
}

textarea:focus,
input:focus,
select:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  align-items: center;
}

.chip {
  border: 1px solid var(--ow-line);
  background: rgba(16, 22, 48, 0.6);
  border-radius: 999px;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.8rem;
  padding: 0.22rem 0.7rem;
  cursor: pointer;
}

.chip.on {
  border-color: var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
}

.chips.static .chip {
  cursor: default;
}

.chip.amber {
  border-color: rgba(224, 180, 106, 0.45);
  color: #e6c07e;
}

.chip.red {
  border-color: rgba(224, 133, 133, 0.45);
  color: #e89a9a;
}

button.primary {
  background: linear-gradient(180deg, #f0d28a 0%, #b9924a 100%);
  border: 1px solid rgba(240, 210, 138, 0.65);
  border-radius: 0.5rem;
  color: #241a05;
  font-weight: 600;
  padding: 0.55rem 1rem;
  cursor: pointer;
  align-self: flex-start;
}

button.primary:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.result {
  border-top: 1px solid var(--ow-line);
  padding-top: 0.7rem;
  margin-top: 0.2rem;
}

.r-head {
  display: flex;
  align-items: baseline;
  gap: 0.6rem;
  margin-bottom: 0.3rem;
}

.r-head b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.cost {
  font-family: ui-monospace, Consolas, monospace;
  font-size: 0.74rem;
  color: var(--ow-cyan);
  border: 1px solid rgba(143, 214, 232, 0.35);
  border-radius: 999px;
  padding: 0.1rem 0.5rem;
}

.objective {
  margin: 0 0 0.4rem;
  line-height: 1.6;
}

.stages {
  margin: 0 0 0.4rem;
  padding-left: 1.2rem;
  display: flex;
  flex-direction: column;
  gap: 0.25rem;
  font-size: 0.86rem;
}

.small {
  font-size: 0.78rem;
}

.lines {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin-bottom: 0.4rem;
}

.line {
  font-size: 0.86rem;
  line-height: 1.5;
}

.who {
  color: var(--ow-gold-bright);
  margin-right: 0.5rem;
  font-size: 0.8rem;
}

.cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.5rem;
  margin-bottom: 0.4rem;
}

.fcard {
  border: 1px solid var(--ow-line);
  border-radius: 0.55rem;
  background: var(--ow-panel-2);
  padding: 0.5rem 0.7rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.84rem;
}

.fcard b {
  color: var(--ow-gold-bright);
}
</style>
