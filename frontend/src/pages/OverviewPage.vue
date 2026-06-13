<script setup lang="ts">
import { onMounted, onUnmounted, reactive, ref } from "vue";
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

// count-up: rAF tween toward each tile's real value, honoring reduced-motion
const shown = reactive<Record<string, number>>({});
let raf = 0;

function tweenTo(targets: Record<string, number>): void {
  const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (reduced) {
    Object.assign(shown, targets);
    return;
  }
  const start = performance.now();
  const from: Record<string, number> = { ...shown };
  const duration = 700;
  const step = (now: number): void => {
    const t = Math.min(1, (now - start) / duration);
    const ease = 1 - (1 - t) ** 3;
    for (const key of Object.keys(targets)) {
      shown[key] = Math.round((from[key] ?? 0) + ((targets[key] ?? 0) - (from[key] ?? 0)) * ease);
    }
    if (t < 1) raf = window.requestAnimationFrame(step);
  };
  raf = window.requestAnimationFrame(step);
}

onUnmounted(() => window.cancelAnimationFrame(raf));

onMounted(async () => {
  try {
    const body = await apiGet<{ overview: Overview }>(
      `/projects/${currentProject()}/overview`,
    );
    overview.value = body.overview;
    const targets: Record<string, number> = {};
    for (const key of Object.keys(COUNT_LABELS)) targets[key] = body.overview.counts[key] ?? 0;
    targets.nodes = body.overview.graph.nodes;
    targets.edges = body.overview.graph.edges;
    tweenTo(targets);
  } catch (e) {
    error.value = String(e);
  }
});
</script>

<template>
  <section>
    <div class="section"><span class="t">世界总览</span></div>
    <p v-if="error" class="muted">读取失败：{{ error }}（确认 API 已启动且项目已注册）</p>
    <div v-else-if="!overview" class="tiles">
      <div v-for="i in 8" :key="i" class="tile pane skeleton">
        <span class="sk sk-label"></span>
        <span class="sk sk-value"></span>
      </div>
    </div>
    <template v-else>
      <div class="tiles stagger">
        <div v-for="(label, key) in COUNT_LABELS" :key="key" class="tile pane">
          <span class="label">{{ label }}</span>
          <span class="value">{{ shown[key] ?? 0 }}</span>
        </div>
        <div class="tile pane">
          <span class="label">图谱节点</span>
          <span class="value">{{ shown.nodes ?? 0 }}</span>
        </div>
        <div class="tile pane">
          <span class="label">图谱边</span>
          <span class="value">{{ shown.edges ?? 0 }}</span>
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
  font-variant-numeric: tabular-nums;
}

/* skeleton shimmer while the archive loads */
.sk {
  display: block;
  border-radius: 0.35rem;
  background: linear-gradient(
    100deg,
    rgba(46, 54, 88, 0.45) 40%,
    rgba(217, 181, 108, 0.18) 50%,
    rgba(46, 54, 88, 0.45) 60%
  );
  background-size: 220% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}

.sk-label {
  width: 3.2rem;
  height: 0.78rem;
}

.sk-value {
  width: 2.4rem;
  height: 1.7rem;
  margin-top: 0.2rem;
}

@keyframes shimmer {
  to {
    background-position: -120% 0;
  }
}

/* tiles drift in one after another */
.stagger > .tile {
  animation: rise 0.45s ease both;
}

.stagger > .tile:nth-child(2) {
  animation-delay: 0.05s;
}

.stagger > .tile:nth-child(3) {
  animation-delay: 0.1s;
}

.stagger > .tile:nth-child(4) {
  animation-delay: 0.15s;
}

.stagger > .tile:nth-child(5) {
  animation-delay: 0.2s;
}

.stagger > .tile:nth-child(6) {
  animation-delay: 0.25s;
}

.stagger > .tile:nth-child(7) {
  animation-delay: 0.3s;
}

.stagger > .tile:nth-child(8) {
  animation-delay: 0.35s;
}

@keyframes rise {
  from {
    opacity: 0;
    transform: translateY(8px);
  }

  to {
    opacity: 1;
    transform: translateY(0);
  }
}

@media (prefers-reduced-motion: reduce) {
  .sk,
  .stagger > .tile {
    animation: none;
  }
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
