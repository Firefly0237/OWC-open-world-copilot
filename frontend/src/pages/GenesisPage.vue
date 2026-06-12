<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { apiGet, apiPost, currentProject, streamJobEvents } from "../api";

/** Guided dimensions, distilled from real worldbuilding/story-bible structures
 * (genre, tone, era, magic system, scope, central conflict) plus the content-redline
 * section CN game docs carry. Every one is optional: "暂未想好" sends nothing, "其他…"
 * opens a free-text input — the form guides, never forces. */
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
    placeholder: "例如：蒸汽与符文并行的双轨时代",
  },
  {
    key: "magic_level",
    label: "魔法 / 超自然体系",
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
  restrictions: "",
  notes: "",
  factions: 2,
  regions: 1,
  npcs: 4,
  quests: 2,
  terms: 3,
});

const running = ref(false);
const progress = ref<string[]>([]);
const result = ref<{ summary?: string; counts?: Record<string, number> } | null>(null);
const error = ref("");

const STAGE_LABELS: Record<string, string> = {
  retrieving: "正在检索项目事实与灵感参考…",
  generating: "正在推演世界草案…",
  parsing: "正在整理结构化产物…",
};

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

async function run(): Promise<void> {
  if (!canRun.value) return;
  running.value = true;
  progress.value = [];
  result.value = null;
  error.value = "";
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
    progress.value.push("任务已受理，开始执行…");
    await streamJobEvents(job.job_id, (event) => {
      if (event.type === "stage") {
        const name = String(event.data.name ?? "");
        progress.value.push(STAGE_LABELS[name] ?? name);
      } else if (event.type === "failed") {
        error.value = String(event.data.error ?? "任务失败");
      }
    });
    const status = await apiGet<{
      status: string;
      result: { summary?: string; counts?: Record<string, number> } | null;
      error: string | null;
    }>(`/jobs/${job.job_id}`);
    if (status.status === "done" && status.result) {
      result.value = status.result;
      progress.value.push("草案已写就，正于审阅台候批。");
    } else if (!error.value) {
      error.value = status.error ?? "任务未完成";
    }
  } catch (e) {
    error.value = String(e);
  } finally {
    running.value = false;
  }
}
</script>

<template>
  <section>
    <div class="section"><span class="t">创世工坊 · 一键创世</span></div>
    <p class="muted hint">
      写下核心想法（唯一必填），其余维度按需选择——选「暂未想好」就交给模型自行裁量，
      选「其他…」可自由填写。留空的维度不会进入提示词。
    </p>

    <div class="pane form">
      <label class="field">
        <span class="label">核心想法 <em>必填</em></span>
        <textarea
          v-model="form.idea"
          rows="3"
          placeholder="例如：一个靠蒸汽巨树维持生命的群岛世界，各方势力争夺树心的控制权。——只写这一句也能开辟世界。"
        ></textarea>
      </label>

      <div class="field">
        <span class="label">题材风格 <i class="muted">可多选</i></span>
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
          <input v-model="form.styleCustom" class="chip-input" placeholder="其他风格…" />
        </div>
      </div>

      <div class="grid">
        <label v-for="dim in DIMENSIONS" :key="dim.key" class="field">
          <span class="label">{{ dim.label }}</span>
          <select v-model="form.selections[dim.key]">
            <option :value="UNDECIDED">{{ UNDECIDED }}（交给模型）</option>
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
          <input v-model="form.playerFantasy" placeholder="留空 = 不设主角（纯世界观）" />
        </label>
        <label class="field">
          <span class="label">内容红线（必须避免）</span>
          <input v-model="form.restrictions" placeholder="例如：不出现骸骨与血泊描写" />
        </label>
      </div>

      <label class="field">
        <span class="label">补充要求</span>
        <input v-model="form.notes" placeholder="任何其他叮嘱…" />
      </label>

      <div class="field">
        <span class="label">生成规模 <i class="muted">0 = 完全不要这一类</i></span>
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

    <div v-if="progress.length" class="pane log">
      <div v-for="(line, index) in progress" :key="index" class="line">✦ {{ line }}</div>
    </div>
    <p v-if="error" class="error">{{ error }}</p>
    <div v-if="result" class="pane done">
      <p>{{ result.summary }}</p>
      <div class="chips">
        <span v-for="(count, key) in result.counts ?? {}" :key="key" class="chip static">
          {{ COUNT_LABELS[key] ?? key }} <b>{{ count }}</b>
        </span>
      </div>
      <p class="muted">前往「审阅台」采纳或驳回这份草案。</p>
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
  width: 8.5rem;
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

.log {
  margin-top: 0.9rem;
  padding: 0.8rem 1rem;
  font-size: 0.85rem;
}

.line {
  padding: 0.15rem 0;
  color: var(--ow-cyan);
  animation: ow-fade-up 0.25s ease-out both;
}

.error {
  color: #e89a9a;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.done .chips {
  margin: 0.5rem 0;
}
</style>
