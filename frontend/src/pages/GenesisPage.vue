<script setup lang="ts">
import { computed, onUnmounted, reactive, ref } from "vue";
import { apiGet, apiPost, currentProject, streamJobEvents } from "../api";

const UNDECIDED = "暂未想好";
const CUSTOM = "其他…";

interface Dimension {
  key: string;
  label: string;
  options: string[];
  placeholder: string;
}

const DIMENSIONS: Dimension[] = [
  {
    key: "medium",
    label: "载体 / 媒介",
    options: ["开放世界游戏", "RPG", "视觉小说", "剧本 / 影视", "小说设定", "桌面跑团"],
    placeholder: "例如：互动广播剧",
  },
  {
    key: "tone",
    label: "基调 / 氛围",
    options: ["史诗庄重", "黑暗冷峻", "轻松诙谐", "悬疑诡谲", "温柔治愈", "残酷写实", "浪漫瑰丽"],
    placeholder: "例如：哀而不伤",
  },
  {
    key: "era",
    label: "时代 / 技术水平",
    options: ["上古神话", "古典王朝", "中世纪", "工业革命", "现代都市", "近未来", "远未来星际"],
    placeholder: "例如：蒸汽与符文并行",
  },
  {
    key: "magic_level",
    label: "魔法 / 超自然",
    options: ["无超自然", "低魔（稀有而危险）", "高魔（融入日常）", "科技拟魔", "神明在场", "规则怪谈"],
    placeholder: "例如：以记忆为燃料的咒术",
  },
  {
    key: "world_scale",
    label: "世界尺度",
    options: ["一城一镇", "一国一域", "整片大陆", "整颗星球", "星系 / 星海", "多元位面"],
    placeholder: "例如：一艘世代飞船内部",
  },
  {
    key: "core_conflict",
    label: "核心冲突",
    options: [
      "资源争夺",
      "王权 / 政权更替",
      "种族或阵营对立",
      "灾变求生",
      "旧神 / 古老存在复苏",
      "探索未知边疆",
      "阶级与革命",
    ],
    placeholder: "例如：两种不相容的时间流速",
  },
];

const STYLE_OPTIONS = [
  "奇幻",
  "黑暗奇幻",
  "科幻",
  "赛博朋克",
  "蒸汽朋克",
  "武侠",
  "仙侠",
  "克苏鲁",
  "废土",
  "现代都市",
  "历史架空",
];

const STAGES = [
  { key: "accepted", label: "受理" },
  { key: "retrieving", label: "检索" },
  { key: "generating", label: "推演" },
  { key: "parsing", label: "整理" },
  { key: "done", label: "候批" },
];

const form = reactive({
  idea: "",
  styles: [] as string[],
  styleCustom: "",
  selections: Object.fromEntries(DIMENSIONS.map((d) => [d.key, UNDECIDED])) as Record<
    string,
    string
  >,
  customs: Object.fromEntries(DIMENSIONS.map((d) => [d.key, ""])) as Record<string, string>,
  playerFantasy: "",
  castRows: [] as { name: string; profile: string }[],
  restrictions: "",
  notes: "",
  factions: 2,
  regions: 1,
  npcs: 4,
  quests: 2,
  terms: 3,
});

function addCastRow(): void {
  form.castRows.push({ name: "", profile: "" });
}

function removeCastRow(index: number): void {
  form.castRows.splice(index, 1);
}

interface SeedEntity {
  name: string;
  type: string;
  description: string;
}

interface SeedRelation {
  source: string;
  target: string;
  kind: string;
}

const running = ref(false);
const stageIndex = ref(-1);
const elapsed = ref(0);
const error = ref("");
const result = ref<{
  summary: string;
  counts: Record<string, number>;
  characters: SeedEntity[];
  relations: SeedRelation[];
} | null>(null);

let timer: number | undefined;

const COUNT_LABELS: Record<string, string> = {
  entities: "实体",
  quests: "任务",
  regions: "区域",
  pois: "地点",
  terms: "术语",
  relations: "关系",
  style_guides: "风格圣经",
};

const canRun = computed(() => form.idea.trim().length > 0 && !running.value);

function resolved(key: string): string {
  const pick = form.selections[key];
  if (pick === UNDECIDED) return "";
  if (pick === CUSTOM) return form.customs[key].trim();
  return pick;
}

function toggleStyle(style: string): void {
  const index = form.styles.indexOf(style);
  if (index >= 0) form.styles.splice(index, 1);
  else form.styles.push(style);
}

function stopTimer(): void {
  if (timer !== undefined) {
    window.clearInterval(timer);
    timer = undefined;
  }
}

onUnmounted(stopTimer);

async function run(): Promise<void> {
  if (!canRun.value) return;
  running.value = true;
  stageIndex.value = 0;
  elapsed.value = 0;
  result.value = null;
  error.value = "";
  stopTimer();
  timer = window.setInterval(() => {
    elapsed.value += 1;
  }, 1000);
  const styles = [...form.styles];
  if (form.styleCustom.trim()) styles.push(form.styleCustom.trim());
  try {
    const job = await apiPost<{ job_id: string }>(`/projects/${currentProject()}/jobs`, {
      kind: "world_seed",
      params: {
        brief: {
          idea: form.idea.trim(),
          medium: resolved("medium"),
          world_styles: styles,
          tone: resolved("tone"),
          era: resolved("era"),
          magic_level: resolved("magic_level"),
          world_scale: resolved("world_scale"),
          core_conflict: resolved("core_conflict"),
          player_fantasy: form.playerFantasy.trim(),
          key_characters: form.castRows
            .filter((row) => row.name.trim())
            .map((row) =>
              row.profile.trim() ? `${row.name.trim()}：${row.profile.trim()}` : row.name.trim(),
            ),
          content_restrictions: form.restrictions.trim(),
          notes: form.notes.trim(),
          faction_count: form.factions,
          region_count: form.regions,
          npc_count: form.npcs,
          quest_count: form.quests,
          term_count: form.terms,
        },
      },
    });
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "stage") {
        const name = String(event.data.name ?? "");
        const index = STAGES.findIndex((s) => s.key === name);
        if (index > stageIndex.value) stageIndex.value = index;
      } else if (event.type === "failed") {
        error.value = String(event.data.error ?? "任务失败");
      }
    });
    const status = await apiGet<{
      status: string;
      result: {
        summary?: string;
        counts?: Record<string, number>;
        bundle?: {
          entities?: Record<string, SeedEntity>;
          relations?: SeedRelation[];
        };
      } | null;
      error: string | null;
    }>(`/jobs/${job.job_id}`);
    if (status.status === "done" && status.result) {
      stageIndex.value = STAGES.length - 1;
      const bundle = status.result.bundle ?? {};
      const entities = Object.values(bundle.entities ?? {});
      const names = new Map(
        Object.entries(bundle.entities ?? {}).map(([id, e]) => [id, e.name]),
      );
      result.value = {
        summary: status.result.summary ?? "",
        counts: status.result.counts ?? {},
        characters: entities.filter((e) => e.type === "npc"),
        relations: (bundle.relations ?? []).slice(0, 14).map((r) => ({
          source: names.get(r.source) ?? r.source,
          target: names.get(r.target) ?? r.target,
          kind: r.kind,
        })),
      };
    } else if (!error.value) {
      error.value = status.error ?? "任务未完成";
    }
  } catch (e) {
    error.value = String(e);
  } finally {
    running.value = false;
    stopTimer();
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">创世工坊 · 一键创世</span></div>
    <p class="muted hint">只有核心想法必填，其余想到哪选到哪。</p>

    <div class="pane form">
      <label class="field">
        <span class="label">
          核心想法 <em>必填</em>
          <i class="muted">{{ form.idea.length }}/4000</i>
        </span>
        <textarea
          v-model="form.idea"
          rows="3"
          maxlength="4000"
          placeholder="例如：一个靠蒸汽巨树维持生命的群岛世界，各方势力争夺树心的控制权。"
        ></textarea>
        <span v-if="form.idea.length > 1200" class="muted small">
          篇幅很长？现成的设定文档更适合用「文稿提炼」整理入档，这里写主旨即可。
        </span>
      </label>

      <div class="field">
        <span class="label">题材风格</span>
        <div class="chips">
          <button
            v-for="style in STYLE_OPTIONS"
            :key="style"
            type="button"
            class="chip"
            :class="{ on: form.styles.includes(style) }"
            @click="toggleStyle(style)"
          >
            {{ style }}
          </button>
          <input v-model="form.styleCustom" class="chip-input" placeholder="其他…" />
        </div>
      </div>

      <div class="grid">
        <label v-for="dim in DIMENSIONS" :key="dim.key" class="field">
          <span class="label">{{ dim.label }}</span>
          <select v-model="form.selections[dim.key]">
            <option :value="UNDECIDED">{{ UNDECIDED }}</option>
            <option v-for="option in dim.options" :key="option" :value="option">
              {{ option }}
            </option>
            <option :value="CUSTOM">{{ CUSTOM }}</option>
          </select>
          <input
            v-if="form.selections[dim.key] === CUSTOM"
            v-model="form.customs[dim.key]"
            :placeholder="dim.placeholder"
          />
        </label>
        <label class="field">
          <span class="label">主角 / 玩家身份</span>
          <input v-model="form.playerFantasy" placeholder="可留空" />
        </label>
        <label class="field">
          <span class="label">内容红线</span>
          <input v-model="form.restrictions" placeholder="必须避免的内容，可留空" />
        </label>
      </div>

      <div class="field">
        <span class="label">主要人物</span>
        <div v-for="(row, index) in form.castRows" :key="index" class="cast-row">
          <input v-model="row.name" maxlength="40" placeholder="名字" />
          <input v-model="row.profile" maxlength="200" placeholder="一句设定，例如：守灯二十年的老领航员" />
          <button type="button" class="ghost" @click="removeCastRow(index)">移除</button>
        </div>
        <button type="button" class="ghost add" @click="addCastRow">+ 添加人物</button>
      </div>

      <label class="field">
        <span class="label">补充要求</span>
        <input v-model="form.notes" placeholder="可留空" />
      </label>

      <div class="field">
        <span class="label">生成规模 <i class="muted">0 = 不要这一类</i></span>
        <div class="scales">
          <label v-for="(label, key) in { factions: '阵营', regions: '区域', npcs: '角色', quests: '任务', terms: '术语' }" :key="key">
            <span class="muted">{{ label }} {{ form[key] }}</span>
            <input v-model.number="form[key]" type="range" min="0" :max="key === 'npcs' ? 24 : 8" />
          </label>
        </div>
      </div>

      <button class="primary" :disabled="!canRun" @click="run">
        {{ running ? "正在开辟…" : "开辟世界" }}
      </button>
    </div>

    <div v-if="running || stageIndex >= 0" class="pane progress">
      <svg
        v-if="running"
        class="emblem"
        width="56"
        height="56"
        viewBox="0 0 100 100"
        fill="none"
      >
        <circle class="orb" cx="50" cy="50" r="44" stroke="#8fd6e8" stroke-opacity=".35" stroke-dasharray="3 6" />
        <circle cx="50" cy="50" r="34" stroke="#d9b56c" stroke-opacity=".4" />
        <path
          class="core"
          d="M50 14 L56.5 43.5 L86 50 L56.5 56.5 L50 86 L43.5 56.5 L14 50 L43.5 43.5 Z"
          fill="#d9b56c"
          fill-opacity=".25"
          stroke="#f0d28a"
          stroke-opacity=".8"
        />
      </svg>
      <div class="stages">
        <div class="steps">
          <template v-for="(stage, index) in STAGES" :key="stage.key">
            <span
              class="step"
              :class="{ done: index < stageIndex, active: index === stageIndex && running }"
            >
              {{ stage.label }}
            </span>
            <span v-if="index < STAGES.length - 1" class="step-line"></span>
          </template>
        </div>
        <span v-if="running" class="muted elapsed">已用时 {{ elapsed }}s · 大世界通常需要一两分钟</span>
      </div>
    </div>

    <p v-if="error" class="error">{{ error }}</p>

    <div v-if="result" class="pane done">
      <p class="summary">{{ result.summary }}</p>
      <div class="chips">
        <span v-for="(count, key) in result.counts" :key="key" class="chip static">
          {{ COUNT_LABELS[key] ?? key }} <b>{{ count }}</b>
        </span>
      </div>
      <template v-if="result.characters.length">
        <div class="section"><span class="t">人物</span></div>
        <div class="cast">
          <div v-for="person in result.characters" :key="person.name" class="person">
            <b>{{ person.name }}</b>
            <span class="muted">{{ person.description }}</span>
          </div>
        </div>
      </template>
      <template v-if="result.relations.length">
        <div class="section"><span class="t">关系</span></div>
        <div class="relations">
          <span v-for="(rel, index) in result.relations" :key="index" class="rel">
            {{ rel.source }} <i>{{ rel.kind }}</i> {{ rel.target }}
          </span>
        </div>
      </template>
      <p class="muted">草案已入审阅台，采纳后正式入档。</p>
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

select:focus,
input:focus,
textarea:focus {
  outline: none;
  border-color: var(--ow-gold-soft);
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 0.8rem;
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
  font-size: 0.8rem;
  padding: 0.22rem 0.7rem;
  cursor: pointer;
  transition:
    border-color 0.15s ease,
    color 0.15s ease,
    background 0.15s ease;
}

.chip:hover {
  border-color: var(--ow-gold-soft);
}

.chip.on {
  border-color: var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  color: var(--ow-gold-bright);
}

.chip.static {
  cursor: default;
}

.chip.static b {
  color: var(--ow-ink);
}

.chip-input {
  border-radius: 999px;
  font-size: 0.8rem;
  padding: 0.22rem 0.7rem;
  width: 7rem;
}

.cast-row {
  display: grid;
  grid-template-columns: 9rem 1fr auto;
  gap: 0.5rem;
  margin-bottom: 0.45rem;
}

button.ghost {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-muted);
  font: inherit;
  font-size: 0.82rem;
  padding: 0.4rem 0.8rem;
  cursor: pointer;
}

button.ghost:hover {
  border-color: var(--ow-gold-soft);
  color: var(--ow-ink);
}

button.add {
  align-self: flex-start;
}

.small {
  font-size: 0.78rem;
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
  border-radius: 0.5rem;
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

.progress {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
  display: flex;
  align-items: center;
  gap: 1rem;
}

.emblem .orb {
  animation: spin 10s linear infinite;
  transform-origin: center;
  transform-box: fill-box;
}

.emblem .core {
  animation: pulse 2s ease-in-out infinite;
  transform-origin: center;
  transform-box: fill-box;
}

@keyframes spin {
  to {
    transform: rotate(360deg);
  }
}

@keyframes pulse {
  0%,
  100% {
    opacity: 0.7;
    transform: scale(0.96);
  }

  50% {
    opacity: 1;
    transform: scale(1.05);
  }
}

.stages {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
}

.steps {
  display: flex;
  align-items: center;
  gap: 0.45rem;
}

.step {
  font-size: 0.84rem;
  color: var(--ow-muted);
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  padding: 0.18rem 0.7rem;
  transition:
    color 0.2s ease,
    border-color 0.2s ease,
    box-shadow 0.2s ease;
}

.step.done {
  color: var(--ow-cyan);
  border-color: rgba(143, 214, 232, 0.4);
}

.step.active {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.35);
}

.step-line {
  width: 1.1rem;
  height: 1px;
  background: linear-gradient(90deg, var(--ow-gold-soft), transparent);
}

.elapsed {
  font-size: 0.78rem;
}

.error {
  color: #e89a9a;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.summary {
  margin: 0 0 0.5rem;
  line-height: 1.7;
}

.done .chips {
  margin-bottom: 0.4rem;
}

.cast {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(230px, 1fr));
  gap: 0.6rem;
  margin-bottom: 0.4rem;
}

.person {
  border: 1px solid var(--ow-line);
  border-radius: 0.6rem;
  background: var(--ow-panel-2);
  padding: 0.55rem 0.75rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  font-size: 0.85rem;
}

.person b {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
}

.relations {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin-bottom: 0.4rem;
}

.rel {
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  background: rgba(16, 22, 48, 0.6);
  font-size: 0.78rem;
  padding: 0.18rem 0.65rem;
}

.rel i {
  color: var(--ow-cyan);
  font-style: normal;
  margin: 0 0.3rem;
}
</style>
