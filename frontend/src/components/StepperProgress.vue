<script setup lang="ts">
import { computed } from "vue";
import StarEmblem from "./StarEmblem.vue";

const props = defineProps<{
  stages: { key: string; label: string }[];
  index: number;
  running: boolean;
  elapsed: number;
  hint?: string;
  /** optional evocative per-stage line, shown while running (animation copy — creative is OK here) */
  flavors?: Record<string, string>;
}>();

// determinate fill: completed stages, plus a half-segment for the one in flight, so the rail moves
// from the very first stage instead of sitting at 0% while stage one runs.
const pct = computed(() => {
  const total = Math.max(1, props.stages.length);
  const advanced = props.running ? props.index + 0.5 : props.index + 1;
  return Math.max(4, Math.min(100, Math.round((advanced / total) * 100)));
});

const flavor = computed(() => {
  if (!props.running || !props.flavors) return "";
  const stage = props.stages[Math.max(0, Math.min(props.index, props.stages.length - 1))];
  return stage ? (props.flavors[stage.key] ?? "") : "";
});
</script>

<template>
  <div class="pane progress" :class="{ live: running }">
    <StarEmblem v-if="running" :size="54" />
    <div class="stages">
      <div class="steps">
        <template v-for="(stage, i) in stages" :key="stage.key">
          <span class="step" :class="{ done: i < index, active: i === index && running }">
            {{ stage.label }}
          </span>
          <span v-if="i < stages.length - 1" class="step-line" :class="{ lit: i < index }"></span>
        </template>
      </div>
      <!-- the star rail: a determinate fill with a bright travelling head + flowing streak -->
      <div class="rail" :class="{ running }">
        <span class="rail-fill" :style="{ width: pct + '%' }">
          <i class="rail-head"></i>
        </span>
      </div>
      <span v-if="running" class="muted elapsed">
        已用时 {{ elapsed }}s · {{ pct }}%{{ hint ? ` · ${hint}` : "" }}
      </span>
      <span v-if="flavor" class="flavor">{{ flavor }}</span>
    </div>
  </div>
</template>

<style scoped>
.progress {
  margin-top: 0.9rem;
  padding: 0.85rem 1.05rem;
  display: flex;
  align-items: center;
  gap: 1rem;
}
.progress.live {
  border-color: var(--ow-violet-soft);
}

.stages {
  display: flex;
  flex-direction: column;
  gap: 0.5rem;
  flex: 1;
  min-width: 0;
}

.steps {
  display: flex;
  align-items: center;
  gap: 0.45rem;
  flex-wrap: wrap;
}

.step {
  font-size: 0.84rem;
  color: var(--ow-muted);
  border: 1px solid var(--ow-line);
  border-radius: 999px;
  padding: 0.18rem 0.7rem;
  transition: color 0.3s ease, border-color 0.3s ease, box-shadow 0.3s ease, background 0.3s ease;
}

.step.done {
  color: var(--ow-cyan);
  border-color: rgba(143, 214, 232, 0.4);
}

.step.active {
  color: var(--ow-gold-bright);
  border-color: var(--ow-gold-soft);
  background: var(--ow-gold-faint);
  box-shadow: 0 0 12px rgba(240, 210, 138, 0.35);
  animation: step-breathe 1.6s ease-in-out infinite;
}

@keyframes step-breathe {
  0%,
  100% {
    box-shadow: 0 0 8px rgba(240, 210, 138, 0.25);
  }
  50% {
    box-shadow: 0 0 18px rgba(240, 210, 138, 0.5);
  }
}

.step-line {
  width: 1.1rem;
  height: 1px;
  background: var(--ow-line);
  transition: background 0.3s ease;
}
.step-line.lit {
  background: linear-gradient(90deg, rgba(143, 214, 232, 0.7), var(--ow-gold-soft));
}

/* the star rail */
.rail {
  position: relative;
  height: 8px;
  border-radius: 99px;
  background: rgba(143, 214, 232, 0.13);
  overflow: visible;
}
.rail-fill {
  position: relative;
  display: block;
  height: 100%;
  border-radius: 99px;
  background: linear-gradient(90deg, var(--ow-violet-deep), var(--ow-gold-bright));
  box-shadow: 0 0 10px rgba(240, 210, 138, 0.45);
  /* smooth, eased advance between stages instead of a hard jump */
  transition: width 0.55s cubic-bezier(0.22, 0.8, 0.2, 1);
}
/* a flowing streak travels along the filled portion */
.rail.running .rail-fill::before {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: 99px;
  background: linear-gradient(90deg, transparent, rgba(255, 246, 214, 0.6), transparent);
  background-size: 50% 100%;
  background-repeat: no-repeat;
  animation: rail-streak 1.5s linear infinite;
}
/* the bright travelling head at the leading edge */
.rail-head {
  position: absolute;
  right: -3px;
  top: 50%;
  width: 9px;
  height: 9px;
  margin-top: -4.5px;
  border-radius: 50%;
  background: #fff6d6;
  box-shadow: 0 0 10px rgba(255, 246, 214, 0.95), 0 0 18px rgba(240, 210, 138, 0.6);
}
.rail.running .rail-head {
  animation: rail-head 1.3s ease-in-out infinite;
}
@keyframes rail-streak {
  from {
    background-position: -60% 0;
  }
  to {
    background-position: 160% 0;
  }
}
@keyframes rail-head {
  0%,
  100% {
    transform: scale(0.85);
    opacity: 0.85;
  }
  50% {
    transform: scale(1.2);
    opacity: 1;
  }
}

.elapsed {
  font-size: 0.78rem;
}
.flavor {
  font-family: var(--ow-serif);
  font-size: 0.82rem;
  color: var(--ow-violet);
  letter-spacing: 0.02em;
  animation: ow-flash-in 0.4s ease both;
}

@media (prefers-reduced-motion: reduce) {
  .step.active,
  .rail.running .rail-fill::before,
  .rail.running .rail-head {
    animation: none;
  }
  .rail-fill {
    transition: none;
  }
}
</style>
