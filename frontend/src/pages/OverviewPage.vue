<script setup lang="ts">
import { onMounted, ref } from "vue";
import { apiGet, currentProject } from "../api";

interface Overview {
  counts: Record<string, number>;
  graph: { nodes: number; edges: number };
  provenance?: { by_origin?: Record<string, number>; by_review_status?: Record<string, number> };
}

const overview = ref<Overview | null>(null);
const error = ref("");

const COUNT_LABELS: Record<string, string> = {
  entities: "实体",
  quests: "任务",
  regions: "区域",
  relations: "关系",
  pois: "兴趣点",
  dialogues: "对白",
};

onMounted(async () => {
  try {
    const body = await apiGet<{ overview: Overview }>(
      `/projects/${currentProject()}/overview`,
    );
    overview.value = body.overview;
  } catch (e) {
    error.value = String(e);
  }
});
</script>

<template>
  <section>
    <div class="section"><span class="t">世界总览</span></div>
    <p v-if="error" class="muted">读取失败：{{ error }}（确认 API 已启动且项目已注册）</p>
    <div v-else-if="!overview" class="muted">正在展卷…</div>
    <template v-else>
      <div class="tiles">
        <div v-for="(label, key) in COUNT_LABELS" :key="key" class="tile pane">
          <span class="label">{{ label }}</span>
          <span class="value">{{ overview.counts[key] ?? 0 }}</span>
        </div>
        <div class="tile pane">
          <span class="label">图谱节点</span>
          <span class="value">{{ overview.graph.nodes }}</span>
        </div>
        <div class="tile pane">
          <span class="label">图谱边</span>
          <span class="value">{{ overview.graph.edges }}</span>
        </div>
      </div>
      <div class="section"><span class="t">内容溯源</span></div>
      <div class="chips">
        <span
          v-for="(count, origin) in overview.provenance?.by_origin ?? {}"
          :key="origin"
          class="chip"
        >
          {{ origin }} <b>{{ count }}</b>
        </span>
        <span
          v-for="(count, status) in overview.provenance?.by_review_status ?? {}"
          :key="status"
          class="chip"
        >
          {{ status }} <b>{{ count }}</b>
        </span>
      </div>
    </template>
  </section>
</template>

<style scoped>
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.7rem;
  margin-bottom: 1.1rem;
}

.tile {
  padding: 0.75rem 0.95rem 0.6rem;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
}

.label {
  color: var(--ow-muted);
  font-size: 0.78rem;
  letter-spacing: 0.08em;
}

.value {
  color: var(--ow-gold-bright);
  font-family: var(--ow-serif);
  font-size: 1.7rem;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 0.4rem;
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
