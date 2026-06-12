<script setup lang="ts">
import { reactive, ref } from "vue";
import { apiGet, apiPost, currentProject, streamJobEvents } from "../api";

interface JobCreated {
  job_id: string;
  status: string;
}

interface SeedResult {
  summary?: string;
  counts?: Record<string, number>;
  review_item_id?: string;
}

const form = reactive({
  idea: "",
  factions: 2,
  regions: 1,
  npcs: 4,
  quests: 2,
  terms: 3,
});
const running = ref(false);
const progress = ref<string[]>([]);
const result = ref<SeedResult | null>(null);
const error = ref("");

const STAGE_LABELS: Record<string, string> = {
  retrieving: "正在检索项目事实与灵感参考…",
  generating: "正在推演世界草案…",
  parsing: "正在整理结构化产物…",
};

async function run(): Promise<void> {
  if (!form.idea.trim() || running.value) return;
  running.value = true;
  progress.value = [];
  result.value = null;
  error.value = "";
  try {
    const job = await apiPost<JobCreated>(`/projects/${currentProject()}/jobs`, {
      kind: "world_seed",
      params: {
        brief: {
          idea: form.idea.trim(),
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
    const status = await apiGet<{ status: string; result: SeedResult | null; error: string }>(
      `/jobs/${job.job_id}`,
    );
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
      只有核心想法是必填——未提及的维度交给模型自行裁量；规模 0 = 完全不要这一类。
      进度经由任务流（SSE）实时回传。
    </p>
    <div class="pane form">
      <textarea
        v-model="form.idea"
        rows="3"
        placeholder="例如：一个靠蒸汽巨树维持生命的群岛世界，各方势力争夺树心的控制权。"
      ></textarea>
      <div class="scales">
        <label v-for="(label, key) in { factions: '阵营', regions: '区域', npcs: '角色', quests: '任务', terms: '术语' }" :key="key">
          <span class="muted">{{ label }} {{ form[key] }}</span>
          <input v-model.number="form[key]" type="range" min="0" :max="key === 'npcs' ? 24 : 8" />
        </label>
      </div>
      <button class="primary" :disabled="running || !form.idea.trim()" @click="run">
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
        <span v-for="(count, key) in result.counts ?? {}" :key="key" class="chip">
          {{ key }} <b>{{ count }}</b>
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
  padding: 1rem 1.1rem;
  display: flex;
  flex-direction: column;
  gap: 0.9rem;
}

textarea {
  background: var(--ow-panel-2);
  border: 1px solid var(--ow-line);
  border-radius: 0.5rem;
  color: var(--ow-ink);
  padding: 0.6rem 0.7rem;
  resize: vertical;
  font: inherit;
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
  padding: 0.55rem 1rem;
  cursor: pointer;
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
}

.error {
  color: #e89a9a;
}

.done {
  margin-top: 0.9rem;
  padding: 0.9rem 1.1rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
  margin: 0.5rem 0;
}

.chip {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  border-radius: 999px;
  color: var(--ow-gold-bright);
  font-size: 0.78rem;
  padding: 0.14rem 0.62rem;
}

.chip b {
  color: var(--ow-ink);
}
</style>
