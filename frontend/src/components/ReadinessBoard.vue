<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { humanizeError, apiGet, currentProject } from "../api";

// Design-readiness = completeness ("做完没有")，与一致性校勘的正确性("对不对")分开。
// 只读、零模型成本——所以这里不带 llmParams，直接 apiGet。
interface KindSummary {
  kind: string;
  total: number;
  ready: number;
  average_score: number;
}
interface ItemReadiness {
  ref: string;
  kind: string;
  name: string;
  score: number;
  ready: boolean;
  missing: string[];
}
interface ReadinessReport {
  standard_version: string;
  total_items: number;
  ready_items: number;
  overall_score: number;
  ready_rate: number;
  by_kind: KindSummary[];
  items: ItemReadiness[];
}

const KIND_LABELS: Record<string, string> = {
  quest: "任务",
  character: "角色",
  faction: "势力",
  region: "区域",
  poi: "地点",
  term: "词条",
  dialogue_tree: "对话树",
};

const report = ref<ReadinessReport | null>(null);
const error = ref("");
const expanded = ref(false);

const RING_R = 46;
const RING_C = 2 * Math.PI * RING_R;
const ringRatio = ref(0); // tweened 0 → overall_score
let raf = 0;

const ringPercent = computed(() => Math.round(ringRatio.value * 100));
const ringOffset = computed(() => RING_C * (1 - ringRatio.value));
// a glowing head sits at the END of the filled arc (inside the -90°-rotated svg, so it tracks the
// gold edge). This is the dynamic gold effect — confined to the filled part, never the empty track.
const headPos = computed(() => {
  const a = ringRatio.value * 2 * Math.PI;
  return { x: 54 + RING_R * Math.cos(a), y: 54 + RING_R * Math.sin(a) };
});
const incomplete = computed(() => (report.value?.items ?? []).filter((it) => !it.ready));

// gold when on track, amber mid, soft red when far — same palette the rest of the app uses
const ringColor = computed(() => {
  const s = report.value?.overall_score ?? 0;
  if (s >= 0.85) return "#8ed4ac";
  if (s >= 0.6) return "var(--ow-gold-bright)";
  return "#e08585";
});

function tweenRing(target: number): void {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
    ringRatio.value = target;
    return;
  }
  const start = performance.now();
  const duration = 800;
  const step = (now: number): void => {
    const t = Math.min(1, (now - start) / duration);
    const ease = 1 - (1 - t) ** 3;
    ringRatio.value = target * ease;
    if (t < 1) raf = window.requestAnimationFrame(step);
  };
  raf = window.requestAnimationFrame(step);
}

function pct(part: number, whole: number): number {
  return whole > 0 ? Math.round((part / whole) * 100) : 100;
}

onUnmounted(() => window.cancelAnimationFrame(raf));

onMounted(async () => {
  try {
    const body = await apiGet<{ readiness: ReadinessReport }>(
      `/projects/${currentProject()}/readiness`,
    );
    report.value = body.readiness;
    tweenRing(body.readiness.overall_score);
  } catch (e) {
    error.value = humanizeError(e);
  }
});
</script>

<template>
  <div class="section"><span class="t">设计就绪度</span></div>
  <p class="muted hint">衡量每条内容的完整度，标出离可量产还差哪几项。</p>

  <p v-if="error" class="muted">读取失败：{{ error }}</p>

  <div v-else-if="!report" class="board pane skeleton">
    <span class="sk sk-ring"></span>
    <div class="sk-rows">
      <span class="sk sk-row"></span>
      <span class="sk sk-row"></span>
      <span class="sk sk-row"></span>
    </div>
  </div>

  <template v-else>
    <div v-if="report.total_items === 0" class="board pane empty">
      <p class="muted">尚无可评估的内容。写下第一个任务或角色后，这里会标出离可量产还差哪几项。</p>
    </div>

    <div v-else class="board pane">
      <div class="ring-wrap" role="img" :aria-label="`总体就绪度 ${ringPercent}%`">
        <svg viewBox="0 0 108 108" class="ring">
          <circle class="track" cx="54" cy="54" :r="RING_R" />
          <circle
            class="prog"
            cx="54"
            cy="54"
            :r="RING_R"
            :stroke="ringColor"
            :stroke-dasharray="RING_C"
            :stroke-dashoffset="ringOffset"
          />
          <circle class="prog-head" :cx="headPos.x" :cy="headPos.y" r="5.5" :fill="ringColor" />
        </svg>
        <div class="ring-center">
          <span class="pct" :style="{ color: ringColor }">{{ ringPercent }}%</span>
          <span class="ring-sub">总体就绪</span>
        </div>
      </div>

      <div class="kinds">
        <div class="kind-head">
          <span>已就绪 <b>{{ report.ready_items }}</b> / {{ report.total_items }} 项</span>
          <span class="std muted">标准 {{ report.standard_version }}</span>
        </div>
        <div v-for="k in report.by_kind" :key="k.kind" class="kind-row">
          <span class="kind-label">{{ KIND_LABELS[k.kind] ?? k.kind }}</span>
          <div class="bar">
            <i :style="{ width: pct(k.ready, k.total) + '%' }"></i>
          </div>
          <span class="kind-count" :class="{ done: k.ready === k.total }">
            {{ k.ready }}/{{ k.total }}
          </span>
        </div>
      </div>
    </div>

    <div v-if="report.total_items > 0" class="drawer-bar">
      <button
        v-if="incomplete.length"
        class="drawer-toggle"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        <span class="caret" :class="{ open: expanded }">▸</span>
        查看未就绪项（{{ incomplete.length }}）
      </button>
      <span v-else class="all-ready">✦ 全部内容均已达到可量产标准</span>
    </div>

    <Transition name="drawer">
      <ul v-if="expanded && incomplete.length" class="missing-list">
        <li v-for="item in incomplete" :key="item.ref" class="missing-item pane">
          <div class="mi-head">
            <span class="mi-kind">{{ KIND_LABELS[item.kind] ?? item.kind }}</span>
            <span class="mi-name">{{ item.name }}</span>
            <span class="mi-ref muted">{{ item.ref }}</span>
          </div>
          <div class="mi-missing">
            <span v-for="m in item.missing" :key="m" class="gap">缺 {{ m }}</span>
          </div>
        </li>
      </ul>
    </Transition>
  </template>
</template>

<style scoped>
.hint {
  font-size: 0.78rem;
  margin: -0.2rem 0 0.6rem;
}

.board {
  display: flex;
  align-items: center;
  gap: 1.6rem;
  padding: 1.1rem 1.3rem;
}

.board.empty {
  justify-content: center;
  text-align: center;
}

.ring-wrap {
  position: relative;
  width: 132px;
  height: 132px;
  flex: none;
}

.ring {
  width: 100%;
  height: 100%;
  transform: rotate(-90deg);
}

.ring .track {
  fill: none;
  stroke: var(--ow-line);
  stroke-width: 8;
}

.ring .prog {
  fill: none;
  stroke-width: 8;
  stroke-linecap: round;
  filter: drop-shadow(0 0 6px rgba(240, 210, 138, 0.4));
  transition: stroke-dashoffset 0.2s linear;
  animation: ring-glow 3s ease-in-out infinite;
}
.ring .prog-head {
  filter: drop-shadow(0 0 7px rgba(255, 246, 214, 0.95));
  transform-origin: center;
  transform-box: fill-box;
  animation: head-pulse 1.8s ease-in-out infinite;
}
@keyframes head-pulse {
  0%,
  100% {
    transform: scale(0.78);
    opacity: 0.85;
  }
  50% {
    transform: scale(1.25);
    opacity: 1;
  }
}
@keyframes ring-glow {
  0%,
  100% {
    filter: drop-shadow(0 0 5px rgba(240, 210, 138, 0.35));
  }
  50% {
    filter: drop-shadow(0 0 13px rgba(240, 210, 138, 0.62));
  }
}

.ring-center {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 0.1rem;
}

.pct {
  font-family: var(--ow-serif);
  font-size: 1.85rem;
  font-variant-numeric: tabular-nums;
  line-height: 1;
}

.ring-sub {
  font-size: 0.72rem;
  letter-spacing: 0.08em;
  color: var(--ow-muted);
}

.kinds {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.kind-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  font-size: 0.82rem;
  margin-bottom: 0.1rem;
}

.kind-head b {
  color: var(--ow-gold-bright);
  font-size: 1rem;
}

.std {
  font-size: 0.72rem;
}

.kind-row {
  display: grid;
  grid-template-columns: 3.2rem 1fr 2.8rem;
  align-items: center;
  gap: 0.6rem;
}

.kind-label {
  font-size: 0.82rem;
  color: var(--ow-ink);
}

.bar {
  height: 7px;
  border-radius: 999px;
  background: var(--ow-panel-2);
  /* no overflow:hidden — let the fill's rounded head + glow spill, killing the hard square edge */
}

.bar i {
  position: relative;
  display: block;
  height: 100%;
  border-radius: 999px;
  background: linear-gradient(90deg, var(--ow-gold-deep), var(--ow-gold-bright));
  box-shadow:
    0 0 8px rgba(240, 210, 138, 0.55),
    0 0 2px rgba(255, 246, 214, 0.9);
  transition: width 0.6s cubic-bezier(0.2, 0.8, 0.2, 1);
}
/* a bright living head at the leading edge — the gold reads as light, not a static fill */
.bar i::after {
  content: "";
  position: absolute;
  right: -1px;
  top: 50%;
  width: 7px;
  height: 7px;
  margin-top: -3.5px;
  border-radius: 50%;
  background: #fff6d6;
  box-shadow: 0 0 9px rgba(255, 246, 214, 0.95);
  animation: bar-head 2.2s ease-in-out infinite;
}
@keyframes bar-head {
  0%,
  100% {
    opacity: 0.7;
    transform: scale(0.85);
  }
  50% {
    opacity: 1;
    transform: scale(1.15);
  }
}

.kind-count {
  font-size: 0.8rem;
  font-variant-numeric: tabular-nums;
  text-align: right;
  color: var(--ow-muted);
}

.kind-count.done {
  color: #8ed4ac;
}

.drawer-bar {
  margin: 0.7rem 0 0.2rem;
}

.drawer-toggle {
  background: transparent;
  border: 1px solid var(--ow-gold-soft);
  border-radius: var(--ow-control-radius);
  color: var(--ow-gold-bright);
  font: inherit;
  font-size: 0.82rem;
  padding: 0.32rem 0.7rem;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  transition: box-shadow 0.2s ease, border-color 0.15s ease;
}

.drawer-toggle:hover {
  border-color: var(--ow-gold);
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.25);
}

.caret {
  display: inline-block;
  transition: transform 0.2s ease;
}

.caret.open {
  transform: rotate(90deg);
}

.all-ready {
  color: #8ed4ac;
  font-size: 0.84rem;
}

.missing-list {
  list-style: none;
  margin: 0.6rem 0 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
}

.missing-item {
  padding: 0.6rem 0.8rem;
}

.mi-head {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  flex-wrap: wrap;
}

.mi-kind {
  border: 1px solid var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  border-radius: 3px;
  clip-path: polygon(
    var(--ow-chip-nip) 0, 100% 0, 100% calc(100% - var(--ow-chip-nip)),
    calc(100% - var(--ow-chip-nip)) 100%, 0 100%, 0 var(--ow-chip-nip)
  );
  color: var(--ow-gold-bright);
  font-size: 0.72rem;
  padding: 0.04rem 0.5rem;
}

.mi-name {
  font-family: var(--ow-serif);
  color: var(--ow-ink);
  font-size: 0.92rem;
}

.mi-ref {
  font-size: 0.72rem;
  font-family: ui-monospace, Consolas, monospace;
}

.mi-missing {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
  margin-top: 0.4rem;
}

.gap {
  border: 1px solid rgba(224, 133, 133, 0.4);
  background: rgba(224, 133, 133, 0.08);
  color: #e6a3a3;
  border-radius: 0.4rem;
  font-size: 0.74rem;
  padding: 0.08rem 0.45rem;
}

/* skeleton */
.skeleton {
  pointer-events: none;
}

.sk {
  display: block;
  border-radius: var(--ow-control-radius);
  background: linear-gradient(
    100deg,
    rgba(46, 54, 88, 0.45) 40%,
    rgba(217, 181, 108, 0.18) 50%,
    rgba(46, 54, 88, 0.45) 60%
  );
  background-size: 220% 100%;
  animation: shimmer 1.4s ease-in-out infinite;
}

.sk-ring {
  width: 132px;
  height: 132px;
  border-radius: 50%;
  flex: none;
}

.sk-rows {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 0.6rem;
}

.sk-row {
  height: 0.9rem;
  width: 100%;
}

@keyframes shimmer {
  to {
    background-position: -120% 0;
  }
}

.drawer-enter-active,
.drawer-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}

.drawer-enter-from,
.drawer-leave-to {
  opacity: 0;
  transform: translateY(-6px);
}

@media (prefers-reduced-motion: reduce) {
  .sk,
  .bar i,
  .bar i::after,
  .prog,
  .prog-head,
  .caret,
  .drawer-enter-active,
  .drawer-leave-active {
    animation: none;
    transition: none;
  }
}

@media (max-width: 560px) {
  .board {
    flex-direction: column;
    gap: 1rem;
  }
}
</style>
